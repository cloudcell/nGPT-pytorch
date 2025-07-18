from __future__ import annotations

from functools import partial

import torch
from torch import nn, stack
from torch.nn import Module, ModuleList
import torch.nn.functional as F
from torch.nn.utils.parametrize import register_parametrization

from einops import rearrange, einsum
from einops.layers.torch import Rearrange

from rotary_embedding_torch import RotaryEmbedding

# constants

from torch.nn.attention import SDPBackend

SDP_BACKEND_MAP = dict(
    enable_flash = SDPBackend.FLASH_ATTENTION,
    enable_mem_efficient = SDPBackend.EFFICIENT_ATTENTION,
    enable_math = SDPBackend.MATH,
    enable_cudnn = SDPBackend.CUDNN_ATTENTION
)

# functions

def exists(v):
    return v is not None

def default(v, d):
    return v if exists(v) else d

def cast_tuple(t, length = 1):
    out = t if isinstance(t, tuple) else ((t,) * length)
    assert len(out) == length
    return out

def l2norm(
    t,
    dim = -1,
    norm_eps = 0.,
    eps = None,
    groups = 1
):
    if groups > 1:
        t = t.chunk(groups, dim = dim)
        t = stack(t)

    if norm_eps == 0.:
        out = F.normalize(t, dim = dim, p = 2)
    else:
        eps = default(eps, 1e-5 if t.dtype == torch.float16 else 1e-10)
        norm = t.norm(dim = dim, keepdim = True)
        target_norm = norm.detach().clamp(min = 1. - norm_eps, max = 1. + norm_eps)
        divisor = norm / target_norm
        out = t / divisor.clamp(min = eps)

    if groups > 1:
        out = torch.cat([*out], dim = dim)

    return out

# scale

class Scale(Module):
    """
    latter part of section 2.5 in the paper
    """
    def __init__(
        self,
        dim,
        init = 1.,
        scale = 1.
    ):
        super().__init__()
        self.dim = dim
        self.scale = nn.Parameter(torch.ones(dim) * scale)
        self.forward_scale = init / scale

    def __eq__(self, other):
        return (
            isinstance(other, Scale) and
            self.dim == other.dim
        )

    def forward(self):
        return self.scale * self.forward_scale

# residual slerp update with learned scale

class Residual(Module):
    def __init__(
        self,
        fn: Module,
        dim: int,
        init: float,
        scale: float | None = None,
        groups = 1,
        norm_eps = 0.
    ):
        super().__init__()
        self.fn = fn
        self.branch_scale = Scale(dim, init, default(scale, dim ** -0.5))
        self.l2norm = L2Norm(dim = -1, norm_eps = norm_eps, groups = groups)

    def forward(self, x, **kwargs):
        residual = x

        out = self.fn(x, **kwargs)

        tuple_output = isinstance(out, tuple)

        if tuple_output:
            out, *rest = out

        out = self.l2norm(out)
        out = self.l2norm(residual.lerp(out, self.branch_scale()))

        if tuple_output:
            out = (out, *rest)

        return out

# for use with parametrize

class L2Norm(Module):
    def __init__(self, dim = -1, norm_eps = 0., groups = 1):
        super().__init__()
        self.dim = dim
        self.norm_eps = norm_eps
        self.groups = groups

    def forward(self, t):
        return l2norm(t, dim = self.dim, norm_eps = self.norm_eps, groups = self.groups)

class NormLinear(Module):
    def __init__(
        self,
        dim,
        dim_out,
        norm_dim_in = True,
        parametrize = True,
        norm_eps = 0.,
        groups = 1
    ):
        super().__init__()
        self.dim = dim
        self.dim_out = dim_out

        self.linear = nn.Linear(dim, dim_out, bias = False)

        self.scale = groups ** -1
        self.parametrize = parametrize
        self.l2norm = L2Norm(dim = -1 if norm_dim_in else 0, norm_eps = norm_eps, groups = groups)

        if parametrize:
            register_parametrization(
                self.linear,
                'weight',
                self.l2norm
            )

        self.norm_weights_()

    def __eq__(self, x):
        return (
            isinstance(x, NormLinear) and
            self.dim == x.dim and
            self.dim_out == x.dim_out
        )

    @torch.no_grad()
    def norm_weights_(self):
        if self.parametrize:
            normed = self.weight
            original = self.linear.parametrizations.weight.original

            original.copy_(normed)
        else:
            self.weight.copy_(self.l2norm(self.weight))

    @property
    def weight(self):
        return self.linear.weight

    def forward(self, x):
        return self.linear(x) * self.scale

# attention

class Attention(Module):
    def __init__(
        self,
        dim,
        *,
        dim_head = 64,
        heads = 8,
        norm_qk = True,
        causal = True,
        manual_norm_weights = False,
        s_qk_init = 1.,
        s_qk_scale = None,
        flash_kwargs: dict = dict(
            enable_flash = True,
            enable_math = True,
            enable_mem_efficient = True,
            enable_cudnn = True
        ),
        norm_eps = 0.,
        num_hyperspheres = 1,
    ):
        super().__init__()
        self.dim = dim
        self.dim_head = dim_head

        self.heads = heads
        self.causal = causal

        NormLinear_ = partial(NormLinear, parametrize = not manual_norm_weights, norm_eps = norm_eps, groups = num_hyperspheres)
        self.l2norm = partial(l2norm, norm_eps = norm_eps, groups = num_hyperspheres)

        dim_sqrt = dim ** 0.5
        self.dim_sqrt = dim_sqrt
        self.attn_scale = dim_head ** 0.5

        dim_inner = dim_head * heads
        self.to_q = NormLinear_(dim, dim_inner)
        self.to_k = NormLinear_(dim, dim_inner)
        self.to_v = NormLinear_(dim, dim_inner)

        # flash attention related context manager

        sdpa_backends = [SDP_BACKEND_MAP[enable_str] for enable_str, enable in flash_kwargs.items() if enable]
        self.sdpa_context_manager = partial(torch.nn.attention.sdpa_kernel, sdpa_backends)

        # qk rmsnorm + scale

        self.norm_qk = norm_qk
        self.qk_scale = Scale(dim_inner, s_qk_init, default(s_qk_scale, dim ** -1))

        self.split_heads = Rearrange('b n (h d) -> b h n d', h = heads)
        self.merge_heads = Rearrange('b h n d -> b n (h d)')

        self.to_out = NormLinear_(dim_inner, dim, norm_dim_in = False)

    def __eq__(x, y):
        return x.dim == y.dim and x.heads == y.heads and x.dim_head == y.dim_head

    def forward(
        self,
        x,
        mask = None,
        rotary_embed: Module | None = None,
        value_residual = None,
        return_values = False
    ):
        q, k, v = self.to_q(x), self.to_k(x), self.to_v(x)

        # split heads

        q, k, v = map(self.split_heads, (q, k, v))

        # maybe value residual, from resformer paper

        if exists(value_residual):
            v = 0.5 * (v + value_residual)

        # rotary positions

        if exists(rotary_embed):
            q = rotary_embed.rotate_queries_or_keys(q)
            k = rotary_embed.rotate_queries_or_keys(k)

        # maybe query key norm

        if self.norm_qk:
            q, k = map(self.l2norm, (q, k))

        # scaling queries and keys - this would line up with the popular use of qk rmsnorm from google deepmind and now black forest labs - will use multihead rmsnorm

        q = q * rearrange(self.qk_scale(), '(h d) -> h 1 d', h = self.heads)

        # for non-autoregressive masking

        if exists(mask):
            row_all_masked_out = ~mask.any(dim = -1)

            mask = rearrange(mask, 'b j -> b 1 1 j')

        # scale is sqrt(dk)

        with self.sdpa_context_manager():
            out = F.scaled_dot_product_attention(
                q, k, v,
                attn_mask = mask,
                is_causal = self.causal,
                scale = self.attn_scale
            )

        out = self.merge_heads(out)
        out = self.to_out(out)

        if exists(mask) and row_all_masked_out.any():
            out = out.masked_fill(row_all_masked_out[:, None, None], 0.)

        if not return_values:
            return out

        return out, v

# feedforward

class FeedForward(Module):
    def __init__(
        self,
        dim,
        *,
        expand_factor = 4,
        manual_norm_weights = False,
        s_hidden_init = 1.,
        s_hidden_scale = 1.,
        s_gate_init = 1.,
        s_gate_scale = 1.,
        norm_eps = 0.,
        num_hyperspheres = 1
    ):
        super().__init__()
        NormLinear_ = partial(NormLinear, parametrize = not manual_norm_weights, norm_eps = norm_eps, groups = num_hyperspheres)

        self.dim = dim
        self.expand_factor = expand_factor

        dim_inner = int(dim * expand_factor * 2 / 3)

        self.dim_inner = dim_inner

        self.to_hidden = NormLinear_(dim, dim_inner)
        self.to_gate = NormLinear_(dim, dim_inner)

        self.hidden_scale = Scale(dim_inner, s_hidden_init, s_hidden_scale)
        self.gate_scale = Scale(dim_inner, s_gate_init, s_gate_scale)

        self.to_out = NormLinear_(dim_inner, dim, norm_dim_in = False)

    def __eq__(x, y):
        return x.dim == y.dim and x.expand_factor == y.expand_factor

    def forward(self, x):
        hidden, gate = self.to_hidden(x), self.to_gate(x)

        hidden = hidden * self.hidden_scale()
        gate = gate * self.gate_scale() * (self.dim ** 0.5)

        hidden = F.silu(gate) * hidden
        return self.to_out(hidden)

# classes

class nGPT(Module):
    def __init__(
        self,
        *,
        num_tokens,
        dim,
        depth,
        dim_head = 64,
        heads = 8,
        attn_norm_qk = True,  # they say the query/key normalization is optional
        ff_expand_factor = 4.,
        ce_ignore_index = -1,
        manual_norm_weights = False,
        tied_embedding = False,
        num_hyperspheres = 1,
        causal = True,
        add_value_residual = True,
        # below are all the scale related hyperparameters, for controlling effective relative learning rates throughout the network
        alpha_init: float | None = None,  # this would set the alpha init for all residuals, but would be overridden by alpha_attn_init and alpha_ff_init if they are specified
        s_logit_init: float  = 1.,
        s_logit_scale: float | None = None,
        alpha_attn_init: float | tuple[float, ...] | None = None,
        alpha_attn_scale: float | tuple[float, ...] | None = None,
        alpha_ff_init: float | tuple[float, ...] | None = None,
        alpha_ff_scale: float | tuple[float, ...] | None = None,
        s_qk_init: float | tuple[float, ...] = 1.,
        s_qk_scale: float | tuple[float, ...] | None = None,
        s_ff_hidden_init: float | tuple[float, ...] = 1.,
        s_ff_hidden_scale: float | tuple[float, ...] = 1.,
        s_ff_gate_init: float | tuple[float, ...] = 1.,
        s_ff_gate_scale: float | tuple[float, ...] = 1.,
        attn_flash_kwargs: dict = dict(
            enable_flash = True,
            enable_math = True,
            enable_mem_efficient = True
        ),
        norm_eps = 0. # greater than 0 allows the norm to be around (1. - norm_eps) to (1. + norm_eps)
    ):
        super().__init__()
        NormLinear_ = partial(NormLinear, parametrize = not manual_norm_weights, norm_eps = norm_eps, groups = num_hyperspheres)
        self.l2norm = partial(l2norm, norm_eps = norm_eps, groups = num_hyperspheres)

        self.num_tokens = num_tokens
        self.dim = dim
        self.heads = heads
        self.dim_head = dim_head
        self.depth = depth
        self.ff_expand_factor = ff_expand_factor

        self.causal = causal
        alpha_init = default(alpha_init, 1. / depth)

        self.add_value_residual = add_value_residual # https://arxiv.org/abs/2410.17897v1

        self.token_embed = NormLinear_(dim, num_tokens)

        self.rotary_embed = RotaryEmbedding(dim_head)

        self.layers = ModuleList([])

        scale_hparams = (
            alpha_attn_init,
            alpha_attn_scale,
            alpha_ff_init,
            alpha_ff_scale,
            s_qk_init,
            s_qk_scale,
            s_ff_hidden_init,
            s_ff_hidden_scale,
            s_ff_gate_init,
            s_ff_gate_scale
        )

        scale_hparams = tuple(cast_tuple(hparam, depth) for hparam in scale_hparams)

        for (
            alpha_attn_init_,
            alpha_attn_scale_,
            alpha_ff_init_,
            alpha_ff_scale_,
            s_qk_init_,
            s_qk_scale_,
            s_ff_hidden_init_,
            s_ff_hidden_scale_,
            s_ff_gate_init_,
            s_ff_gate_scale_
        ) in zip(*scale_hparams):

            attn = Attention(
                dim,
                dim_head = dim_head,
                heads = heads,
                causal = causal,
                norm_qk = attn_norm_qk,
                manual_norm_weights = manual_norm_weights,
                s_qk_init = s_qk_init_,
                s_qk_scale = s_qk_scale_,
                flash_kwargs = attn_flash_kwargs,
                norm_eps = norm_eps,
                num_hyperspheres = num_hyperspheres
            )

            ff = FeedForward(
                dim,
                expand_factor = ff_expand_factor,
                manual_norm_weights = manual_norm_weights,
                s_hidden_init = s_ff_hidden_init_,
                s_hidden_scale = s_ff_hidden_scale_,
                s_gate_init = s_ff_gate_init_,
                s_gate_scale = s_ff_gate_scale_,
                norm_eps = norm_eps,
                num_hyperspheres = num_hyperspheres
            )

            attn_with_residual = Residual(
                attn,
                dim,
                default(alpha_attn_init_, alpha_init),
                default(alpha_attn_scale_, dim ** -0.5)
            )

            ff_with_residual = Residual(
                ff,
                dim,
                default(alpha_ff_init_, alpha_init),
                default(alpha_ff_scale_, dim ** -0.5)
            )

            self.layers.append(ModuleList([attn_with_residual, ff_with_residual]))

        self.to_logits = NormLinear_(dim, num_tokens) if not tied_embedding else None

        self.logit_scale = Scale(num_tokens, s_logit_init, default(s_logit_scale, dim ** -0.5))

        self.ignore_index = ce_ignore_index

    def __eq__(self, other):
        return (
            isinstance(other, nGPT) and
            self.num_tokens == other.num_tokens and 
            self.dim == other.dim and
            self.dim_head == other.dim_head and
            self.depth == other.depth and
            self.heads == other.heads and
            self.ff_expand_factor == other.ff_expand_factor
        )

    @torch.no_grad()
    def norm_weights_(self):
        for module in self.modules():
            if not isinstance(module, NormLinear):
                continue

            module.norm_weights_()

    def register_step_post_hook(self, optimizer):
        assert hasattr(optimizer, 'register_step_post_hook')

        def hook(*_):
            self.norm_weights_()

        return optimizer.register_step_post_hook(hook)

    def forward(
        self,
        ids,
        mask = None,
        return_loss = False,
        return_hiddens = False
    ):
        token_embed, rotary_embed = self.token_embed.weight, self.rotary_embed

        if return_loss:
            assert self.causal
            ids, labels = ids[:, :-1], ids[:, 1:]

        tokens = token_embed[ids]

        hiddens = [tokens]

        first_values = None

        for attn, ff in self.layers:
            tokens, values = attn(tokens, mask = mask, rotary_embed = rotary_embed, return_values = True, value_residual = first_values if self.add_value_residual else None)

            hiddens.append(tokens)
            first_values = default(first_values, values)

            tokens = ff(tokens)

            hiddens.append(tokens)

        if exists(self.to_logits):
            logits = self.to_logits(tokens)
        else:
            # tied embeddings
            logits = einsum(tokens, token_embed, 'b n d, c d -> b n c')

        logits = logits * self.logit_scale()

        # maybe return hiddens

        if return_hiddens:
            hiddens = stack(hiddens)

        # maybe loss

        if not return_loss:

            if not return_hiddens:
                return logits

            return logits, hiddens

        # autoregressive loss

        loss = F.cross_entropy(
            rearrange(logits, 'b n c -> b c n'),
            labels,
            ignore_index = self.ignore_index
        )

        if not return_hiddens:
            return loss

        return loss, hiddens
