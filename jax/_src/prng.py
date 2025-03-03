# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import abc
from functools import partial
from typing import Any, Callable, Hashable, Iterator, NamedTuple, Sequence
import warnings

import numpy as np

import jax
from jax import lax
from jax import core
from jax import numpy as jnp
from jax import tree_util
from jax.config import config
from jax.dtypes import float0
from jax.interpreters import ad
from jax.interpreters import batching
from jax.interpreters import mlir
from jax.interpreters import pxla
from jax.interpreters import xla

from jax._src import dispatch
from jax._src import dtypes
from jax._src.api import jit, vmap
from jax._src.lax import lax as lax_internal
from jax._src.lax import utils as lax_utils
from jax._src.lib.mlir.dialects import mhlo
from jax._src.numpy import lax_numpy
import jax._src.pretty_printer as pp
from jax._src.util import canonicalize_axis, prod, safe_map, safe_zip

from jax._src.lib import gpu_prng

map, unsafe_map = safe_map, map
zip, unsafe_zip = safe_zip, zip


UINT_DTYPES = {
    8: jnp.uint8, 16: jnp.uint16, 32: jnp.uint32, 64: jnp.uint64}  # type: ignore[has-type]

# -- PRNG implementation interface

class PRNGImpl(NamedTuple):
  """Specifies PRNG key shape and operations.

  A PRNG implementation is determined by a key type ``K`` and a
  collection of functions that operate on such keys. The key type
  ``K`` is an array type with element type uint32 and shape specified
  by ``key_shape``. The type signature of each operations is::

    seed :: int[] -> K
    fold_in :: K -> int[] -> K
    split[n] :: K -> K[n]
    random_bits[shape, bit_width] :: K -> uint<bit_width>[shape]

  A PRNG implementation is adapted to an array-like object of keys
  ``K`` by the ``PRNGKeyArray`` class, which should be created via the
  ``seed_with_impl`` function.
  """
  key_shape: core.Shape
  seed: Callable
  split: Callable
  random_bits: Callable
  fold_in: Callable
  tag: str = '?'

  def __hash__(self) -> int:
    return hash(self.tag)

  def __str__(self) -> str:
    return self.tag

  def pprint(self):
    return (pp.text(f"{self.__class__.__name__} [{self.tag}]:") +
            pp.nest(2, pp.group(pp.brk() + pp.join(pp.brk(), [
              pp.text(f"{k} = {v}") for k, v in self._asdict().items()
            ]))))


# -- PRNG key arrays

def _check_prng_key_data(impl, key_data: jnp.ndarray):
  ndim = len(impl.key_shape)
  if not all(hasattr(key_data, attr) for attr in ['ndim', 'shape', 'dtype']):
    raise TypeError("JAX encountered invalid PRNG key data: expected key_data "
                    f"to have ndim, shape, and dtype attributes. Got {key_data}")
  if key_data.ndim < 1:
    raise TypeError("JAX encountered invalid PRNG key data: expected "
                    f"key_data.ndim >= 1; got ndim={key_data.ndim}")
  if key_data.shape[-ndim:] != impl.key_shape:
    raise TypeError("JAX encountered invalid PRNG key data: expected key_data.shape to "
                    f"end with {impl.key_shape}; got shape={key_data.shape} for impl={impl}")
  if key_data.dtype not in [np.uint32, float0]:
    raise TypeError("JAX encountered invalid PRNG key data: expected key_data.dtype = uint32; "
                    f"got dtype={key_data.dtype}")


class PRNGKeyArrayMeta(abc.ABCMeta):
  """Metaclass for overriding PRNGKeyArray isinstance checks."""

  def __instancecheck__(self, instance):
    try:
      return (hasattr(instance, 'aval') and
              isinstance(instance.aval, core.ShapedArray) and
              type(instance.aval.dtype) is KeyTy)
    except AttributeError:
      super().__instancecheck__(instance)


class PRNGKeyArray(metaclass=PRNGKeyArrayMeta):
  """An array whose elements are PRNG keys.

  This class lifts the definition of a PRNG, provided in the form of a
  ``PRNGImpl``, into an array-like pytree class. Instances of this
  class behave like an array whose base elements are keys, hiding the
  fact that keys are typically arrays (of ``uint32`` dtype) themselves.

  PRNGKeyArrays are also restricted relative to JAX arrays in that
  they do not expose arithmetic operations. They instead expose
  wrapper methods around the PRNG implementation functions (``split``,
  ``random_bits``, ``fold_in``).
  """

  impl: PRNGImpl
  _base_array: jnp.ndarray

  def __init__(self, impl, key_data: Any):
    assert not isinstance(key_data, core.Tracer)
    _check_prng_key_data(impl, key_data)
    self.impl = impl
    self._base_array = key_data

  # TODO(frostig): rename to unsafe_base_array, or just offer base_array attr?
  def unsafe_raw_array(self):
    """Access the raw numerical array that carries underlying key data.

    Returns:
      A uint32 JAX array whose leading dimensions are ``self.shape``.
    """
    return self._base_array

  def block_until_ready(self):
    _ = self._base_array.block_until_ready()
    return self

  @property
  def shape(self):
    return base_arr_shape_to_keys_shape(self.impl, self._base_array.shape)

  @property
  def ndim(self):
    return len(self.shape)

  def _is_scalar(self):
    base_ndim = len(self.impl.key_shape)
    return self._base_array.ndim == base_ndim

  def __len__(self):
    if self._is_scalar():
      raise TypeError('len() of unsized object')
    return len(self._base_array)

  def __iter__(self) -> Iterator['PRNGKeyArray']:
    if self._is_scalar():
      raise TypeError('iteration over a 0-d key array')
    # TODO(frostig): we may want to avoid iteration by slicing because
    # a very common use of iteration is `k1, k2 = split(key)`, and
    # slicing/indexing may be trickier to track for linearity checking
    # purposes. Maybe we can:
    # * introduce an unpack primitive+traceable (also allow direct use)
    # * unpack upfront into shape[0] many keyarray slices
    # * return iter over these unpacked slices
    # Whatever we do, we'll want to do it by overriding
    # ShapedArray._iter when the eltype is KeyTy...
    return (PRNGKeyArray(self.impl, k) for k in iter(self._base_array))

  # TODO(frostig): are all of the stackable methods below (reshape,
  # concat, broadcast_to, expand_dims), and the stackable registration,
  # still needed? If, with some work, none are needed, then do we want
  # to remove stackables altogether? This may be the only application.

  # TODO(frostig): Remove? Overwritten below in particular
  def reshape(self, newshape, order=None) -> 'PRNGKeyArray':
    reshaped_base = jnp.reshape(self._base_array, (*newshape, -1), order=order)
    return PRNGKeyArray(self.impl, reshaped_base)

  def concatenate(self, key_arrs, axis, dtype=None):
    if dtype is not None:
      raise ValueError(
          'dtype argument not supported for concatenating PRNGKeyArray')
    axis = canonicalize_axis(axis, self.ndim)
    arrs = [self._base_array, *[k._base_array for k in key_arrs]]
    return PRNGKeyArray(self.impl, jnp.concatenate(arrs, axis))

  def broadcast_to(self, shape):
    if jnp.ndim(shape) == 0:
      shape = (shape,)
    new_shape = (*shape, *self.impl.key_shape)
    return PRNGKeyArray(
        self.impl, jnp.broadcast_to(self._base_array, new_shape))

  def expand_dims(self, dimensions: Sequence[int]):
    # follows lax.expand_dims, not jnp.expand_dims, so dimensions is a sequence
    ndim_out = self.ndim + len(set(dimensions))
    dimensions = [canonicalize_axis(d, ndim_out) for d in dimensions]
    return PRNGKeyArray(
        self.impl, lax.expand_dims(self._base_array, dimensions))

  def __repr__(self):
    return (f'{self.__class__.__name__}[{self.impl.tag}]'
            f' {{ {self._base_array} }}')

  def pprint(self):
    pp_keys = pp.text('shape = ') + pp.text(str(self.shape))
    pp_impl = pp.text('impl = ') + self.impl.pprint()
    return str(pp.group(
      pp.text('PRNGKeyArray:') +
      pp.nest(2, pp.brk() + pp_keys + pp.brk() + pp_impl)))

  # Hollow defs only for typing purposes, overwritten below
  #
  # TODO(frostig): there may be a better way to do this with
  # `typing.type_check_only`.

  @property
  def T(self)                   -> 'PRNGKeyArray': assert False
  def __getitem__(self, _)      -> 'PRNGKeyArray': assert False
  def ravel(self, *_, **__)     -> 'PRNGKeyArray': assert False
  def squeeze(self, *_, **__)   -> 'PRNGKeyArray': assert False
  def swapaxes(self, *_, **__)  -> 'PRNGKeyArray': assert False
  def take(self, *_, **__)      -> 'PRNGKeyArray': assert False
  def transpose(self, *_, **__) -> 'PRNGKeyArray': assert False
  def flatten(self, *_, **__)   -> 'PRNGKeyArray': assert False


lax_numpy._set_device_array_base_attributes(PRNGKeyArray, include=[
    '__getitem__', 'ravel', 'squeeze', 'swapaxes', 'take', 'reshape',
    'transpose', 'flatten', 'T'])
lax_numpy._register_stackable(PRNGKeyArray)


# TODO(frostig): remove, rerouting callers directly to random_seed
def seed_with_impl(impl: PRNGImpl, seed: int) -> PRNGKeyArray:
  return random_seed(seed, impl=impl)


def keys_shaped_array(impl, shape):
  return core.ShapedArray(shape, KeyTy(impl))

def keys_aval_to_base_arr_aval(keys_aval):
  shape = (*keys_aval.shape, *keys_aval.dtype.impl.key_shape)
  return core.ShapedArray(shape, np.dtype('uint32'))

def base_arr_shape_to_keys_shape(impl, base_arr_shape):
  base_ndim = len(impl.key_shape)
  return base_arr_shape[:-base_ndim]


class KeyTy:
  impl: Hashable  # prng.PRNGImpl. TODO(mattjj,frostig): protocol really
  def __init__(self, impl):
    self.impl = impl
  @property
  def name(self) -> str:
    return f'key<{self.impl.tag}>'
  def __repr__(self) -> str:
    return self.name
  def __eq__(self, other):
    return type(other) is KeyTy and self.impl is other.impl
  def __hash__(self) -> int:
    return hash((self.__class__, self.impl))

  # handlers

  @staticmethod
  def physical_avals(aval):
    return [core.ShapedArray((*aval.shape, *aval.dtype.impl.key_shape),
                             jnp.dtype('uint32'))]

  @staticmethod
  def aval_to_ir_types(aval):
    phys_aval, = KeyTy.physical_avals(aval)
    return mlir.aval_to_ir_types(phys_aval)

  @staticmethod
  def result_handler(sticky_device, aval):
    def handler(_, buf):
      buf.aval = core.ShapedArray(buf.shape, buf.dtype)
      return PRNGKeyArray(aval.dtype.impl, buf)
    return handler

  @staticmethod
  def sharded_result_handler(aval, sharding, indices):
    phys_aval, = KeyTy.physical_avals(aval)
    phys_handler_maker = pxla.local_result_handlers[
        (core.ShapedArray, pxla.OutputType.ShardedDeviceArray)]
    phys_handler = phys_handler_maker(phys_aval, sharding, indices)
    def handler(bufs):
      return PRNGKeyArray(aval.dtype.impl, phys_handler(bufs))
    return handler

  # eltype-polymorphic primitive lowering rules

  @staticmethod
  def empty_mlir(ctx):
    aval_out, = ctx.avals_out
    return mlir.ir_constants(np.zeros(aval_out.dtype.impl.key_shape,
                                      dtype=np.dtype('uint32')))

  @staticmethod
  def slice_mlir(ctx, x, start_indices, limit_indices, strides):
    aval_out, = ctx.avals_out
    key_shape = aval_out.dtype.impl.key_shape
    trailing_zeros = [0] * len(key_shape)
    trailing_ones  = [1] * len(key_shape)
    start_indices = (*start_indices, *trailing_zeros)
    limit_indices = (*limit_indices, *key_shape)
    strides = (*strides, *trailing_ones)
    return mhlo.SliceOp(x,
                        mlir.dense_int_elements(start_indices),
                        mlir.dense_int_elements(limit_indices),
                        mlir.dense_int_elements(strides)).results

  @staticmethod
  def dynamic_slice_mlir(ctx, x, start_indices, slice_sizes):
    aval_out, = ctx.avals_out
    dtype = dtypes.canonicalize_dtype(np.dtype('int64'))
    key_shape = aval_out.dtype.impl.key_shape
    trailing_zeros = [mlir.ir_constant(np.array(0, dtype))] * len(key_shape)
    start_indices = (*start_indices, *trailing_zeros)
    slice_sizes_ = mlir.dense_int_elements((*slice_sizes, *key_shape))
    return mhlo.DynamicSliceOp(x, start_indices, slice_sizes_).results

  @staticmethod
  def dynamic_update_slice_mlir(ctx, x, update, *start_indices):
    aval_out, = ctx.avals_out
    dtype = dtypes.canonicalize_dtype(np.dtype('int64'))
    key_shape = aval_out.dtype.impl.key_shape
    zeros = [mlir.ir_constant(np.array(0, dtype=dtype))] * len(key_shape)
    start_indices = (*start_indices, *zeros)
    return mhlo.DynamicUpdateSliceOp(mlir.aval_to_ir_type(aval_out), x, update,
                                     start_indices).results

  @staticmethod
  def broadcast_in_dim_mlir(ctx, x, *dyn_shape, shape, broadcast_dimensions):
    if dyn_shape: raise NotImplementedError
    aval_out, = ctx.avals_out
    key_shape = aval_out.dtype.impl.key_shape
    trailing_dims = [aval_out.ndim + i for i in range(len(key_shape))]
    broadcast_dimensions = [*broadcast_dimensions, *trailing_dims]
    return mhlo.BroadcastInDimOp(
        mlir.aval_to_ir_type(aval_out), x,
        mlir.dense_int_elements(broadcast_dimensions)).results

  @staticmethod
  def transpose_mlir(ctx, x, *, permutation):
    aval_out, = ctx.avals_out
    key_shape = aval_out.dtype.impl.key_shape
    trailing_dims = [aval_out.ndim + i for i in range(len(key_shape))]
    perm = [*permutation, *trailing_dims]
    return mhlo.TransposeOp(x, mlir.dense_int_elements(perm)).results

  @staticmethod
  def gather_mlir(ctx, x, indices, *,
                  dimension_numbers, slice_sizes, unique_indices,
                  indices_are_sorted, mode, fill_value):
    aval_x, aval_indices = ctx.avals_in
    aval_y, = ctx.avals_out
    key_shape = aval_x.dtype.impl.key_shape
    trailing_offset_dims = [aval_y.ndim + i for i in range(len(key_shape))]
    dimension_numbers = dimension_numbers._replace(
        offset_dims=(*dimension_numbers.offset_dims, *trailing_offset_dims))
    slice_sizes = (*slice_sizes, *key_shape)
    gather_lower = partial(
        lax_internal.slicing._gather_lower, dimension_numbers=dimension_numbers,
        slice_sizes=slice_sizes, unique_indices=unique_indices,
        indices_are_sorted=indices_are_sorted, mode=mode, fill_value=fill_value)
    return mlir.delegate_lowering(
        ctx, gather_lower, x, indices,
        avals_in=[keys_aval_to_base_arr_aval(aval_x), aval_indices],
        avals_out=[keys_aval_to_base_arr_aval(aval_y)])

core.custom_eltypes.add(KeyTy)


core.pytype_aval_mappings[PRNGKeyArray] = (
    lambda x: keys_shaped_array(x.impl, x.shape))

xla.pytype_aval_mappings[PRNGKeyArray] = (
    lambda x: keys_shaped_array(x.impl, x.shape))

xla.canonicalize_dtype_handlers[PRNGKeyArray] = lambda x: x

def device_put_key_array(x: PRNGKeyArray, device):
  return dispatch.device_put(x.unsafe_raw_array(), device)
dispatch.device_put_handlers[PRNGKeyArray] = device_put_key_array

def key_array_shard_arg_handler(x: PRNGKeyArray, devices, indices, mode):
  arr = x.unsafe_raw_array()
  return pxla.shard_arg_handlers[type(arr)](arr, devices, indices, mode)
pxla.shard_arg_handlers[PRNGKeyArray] = key_array_shard_arg_handler

def key_array_constant_handler(x, canonicalize_dtypes):
  arr = x.unsafe_raw_array()
  return mlir.get_constant_handler(type(arr))(arr, canonicalize_dtypes)
mlir.register_constant_handler(PRNGKeyArray, key_array_constant_handler)


# -- primitives

def iterated_vmap_unary(n, f):
  for _ in range(n):
    f = jax.vmap(f)
  return f

# TODO(frostig): Revise the following two functions? These basically
# undo the singleton dimensions added by `batching.defbroadcasting`.
# It works, but introduces some possibly-redundant squeezes. Can we
# borrow from other broadcasting primitives instead?

def squeeze_vmap(f, left):
  def squeeze_vmap_f(x, y):
    if left:
      x = jnp.squeeze(x, axis=0)
      axes = (None, 0)
    else:
      y = jnp.squeeze(y, axis=0)
      axes = (0, None)
    return jax.vmap(f, in_axes=axes, out_axes=0)(x, y)
  return squeeze_vmap_f

def iterated_vmap_binary_bcast(shape1, shape2, f):
  ndim1, ndim2 = len(shape1), len(shape2)
  if ndim1 == ndim2 == 0:
    return f
  if 0 in [ndim1, ndim2]:
    if ndim1 == 0:
      return lambda x, y: iterated_vmap_unary(ndim2, lambda y: f(x, y))(y)
    else:
      return lambda x, y: iterated_vmap_unary(ndim1, lambda x: f(x, y))(x)
  assert len(shape1) == len(shape2)
  for sz1, sz2 in reversed(zip(shape1, shape2)):
    if sz1 == sz2:
      f = jax.vmap(f, out_axes=0)
    else:
      assert sz1 == 1 or sz2 == 1, (sz1, sz2)
      f = squeeze_vmap(f, sz1 == 1)
  return f


def random_seed(seeds, impl):
  # Avoid overflow error in X32 mode by first converting ints to int64.
  # This breaks JIT invariance for large ints, but supports the common
  # use-case of instantiating with Python hashes in X32 mode.
  if isinstance(seeds, int):
    seeds_arr = jnp.asarray(np.int64(seeds))
  else:
    seeds_arr = jnp.asarray(seeds)
  return random_seed_p.bind(seeds_arr, impl=impl)

random_seed_p = core.Primitive('random_seed')
ad.defjvp_zero(random_seed_p)
batching.defvectorized(random_seed_p)

@random_seed_p.def_abstract_eval
def random_seed_abstract_eval(seeds_aval, *, impl):
  return keys_shaped_array(impl, seeds_aval.shape)

@random_seed_p.def_impl
def random_seed_impl(seeds, *, impl):
  base_arr = random_seed_impl_base(seeds, impl=impl)
  return PRNGKeyArray(impl, base_arr)

def random_seed_impl_base(seeds, *, impl):
  seed = iterated_vmap_unary(seeds.ndim, impl.seed)
  return seed(seeds)

def random_seed_lowering(ctx, seeds, *, impl):
  aval, = ctx.avals_in
  seed = iterated_vmap_unary(aval.ndim, impl.seed)
  seed_lowering = mlir.lower_fun(seed, multiple_results=False)
  return mlir.delegate_lowering(
      ctx, seed_lowering, seeds,
      avals_out=map(keys_aval_to_base_arr_aval, ctx.avals_out))

mlir.register_lowering(random_seed_p, random_seed_lowering)


def random_split(keys, count):
  return random_split_p.bind(keys, count=count)

random_split_p = core.Primitive('random_split')
ad.defjvp_zero(random_split_p)
batching.defvectorized(random_split_p)

@random_split_p.def_abstract_eval
def random_split_abstract_eval(keys_aval, *, count):
  return keys_shaped_array(keys_aval.dtype.impl, (*keys_aval.shape, count))

@random_split_p.def_impl
def random_split_impl(keys, *, count):
  base_arr = random_split_impl_base(
      keys.impl, keys.unsafe_raw_array(), keys.ndim, count=count)
  return PRNGKeyArray(keys.impl, base_arr)

def random_split_impl_base(impl, base_arr, keys_ndim, *, count):
  split = iterated_vmap_unary(keys_ndim, lambda k: impl.split(k, count))
  return split(base_arr)

def random_split_lowering(ctx, keys, *, count):
  aval, = ctx.avals_in
  impl = aval.dtype.impl
  split = iterated_vmap_unary(aval.ndim, lambda k: impl.split(k, count))
  split_lowering = mlir.lower_fun(split, multiple_results=False)
  return mlir.delegate_lowering(
      ctx, split_lowering, keys,
      avals_in=[keys_aval_to_base_arr_aval(aval)],
      avals_out=map(keys_aval_to_base_arr_aval, ctx.avals_out))

mlir.register_lowering(random_split_p, random_split_lowering)


def random_fold_in(keys, msgs):
  return random_fold_in_p.bind(keys, jnp.asarray(msgs))

random_fold_in_p = core.Primitive('random_fold_in')
ad.defjvp_zero(random_fold_in_p)
batching.defbroadcasting(random_fold_in_p)

@random_fold_in_p.def_abstract_eval
def random_fold_in_abstract_eval(keys_aval, msgs_aval):
  shape = lax_internal.broadcasting_shape_rule(
      'random_fold_in', keys_aval, msgs_aval)
  named_shape = lax_utils.standard_named_shape_rule(keys_aval, msgs_aval)
  return core.ShapedArray(shape, keys_aval.dtype, named_shape=named_shape)

@random_fold_in_p.def_impl
def random_fold_in_impl(keys, msgs):
  base_arr = random_fold_in_impl_base(
      keys.impl, keys.unsafe_raw_array(), msgs, keys.shape)
  return PRNGKeyArray(keys.impl, base_arr)

def random_fold_in_impl_base(impl, base_arr, msgs, keys_shape):
  fold_in = iterated_vmap_binary_bcast(
      keys_shape, np.shape(msgs), impl.fold_in)
  return fold_in(base_arr, msgs)

def random_fold_in_lowering(ctx, keys, msgs):
  keys_aval, msgs_aval = ctx.avals_in
  impl = keys_aval.dtype.impl
  fold_in = iterated_vmap_binary_bcast(
      keys_aval.shape, msgs_aval.shape, impl.fold_in)
  fold_in_lowering = mlir.lower_fun(fold_in, multiple_results=False)
  return mlir.delegate_lowering(
      ctx, fold_in_lowering, keys, msgs,
      avals_in=[keys_aval_to_base_arr_aval(keys_aval), msgs_aval],
      avals_out=map(keys_aval_to_base_arr_aval, ctx.avals_out))

mlir.register_lowering(random_fold_in_p, random_fold_in_lowering)


def random_bits(keys, bit_width, shape):
  shape = core.as_named_shape(shape)
  for name, size in shape.named_items:
    # TODO(frostig,mattjj,apaszke): Is this real_size check necessary,
    # and is it meant to raise a user-facing ValueError? Should it be
    # an `assert` (or RuntimeError) instead? Why do we check it in
    # calls to `random_bits` instead of a more common paralleism path?
    real_size = lax.psum(1, name)
    if real_size != size:
      raise ValueError(f"The shape of axis {name} was specified as {size}, "
                       f"but it really is {real_size}")
    axis_index = lax.axis_index(name)
    keys = random_fold_in(keys, axis_index)
  return random_bits_p.bind(keys, bit_width=bit_width, shape=shape.positional)

random_bits_p = core.Primitive('random_bits')
ad.defjvp_zero(random_bits_p)
batching.defvectorized(random_bits_p)

@random_bits_p.def_abstract_eval
def random_bits_abstract_eval(keys_aval, *, bit_width, shape):
  out_shape = (*keys_aval.shape, *shape)
  out_dtype = dtypes.dtype(f'uint{bit_width}')
  return core.ShapedArray(out_shape, out_dtype)

@random_bits_p.def_impl
def random_bits_impl(keys, *, bit_width, shape):
  return random_bits_impl_base(keys.impl, keys.unsafe_raw_array(), keys.ndim,
                               bit_width=bit_width, shape=shape)

def random_bits_impl_base(impl, base_arr, keys_ndim, *, bit_width, shape):
  bits = iterated_vmap_unary(
      keys_ndim, lambda k: impl.random_bits(k, bit_width, shape))
  return bits(base_arr)

def random_bits_lowering(ctx, keys, *, bit_width, shape):
  aval, = ctx.avals_in
  impl = aval.dtype.impl
  bits = iterated_vmap_unary(
      aval.ndim, lambda k: impl.random_bits(k, bit_width, shape))
  bits_lowering = mlir.lower_fun(bits, multiple_results=False)
  ctx_new = ctx.replace(avals_in=[keys_aval_to_base_arr_aval(aval)])
  out = bits_lowering(ctx_new, keys)
  ctx.set_tokens_out(ctx_new.tokens_out)
  return out

mlir.register_lowering(random_bits_p, random_bits_lowering)


# The following wrap/unwrap primitives are at least a stopgap for
# backwards compatibility, namely when `config.jax_enable_custom_prng`
# is False. We need to convert key arrays to and from underlying
# uint32 base array, and we may need to do so under a jit. For
# example, we want to support:
#
#   keys = jax.jit(random.split)(key)
#
# where `key` and `keys` are both acceptably old-style uint32 arrays
# so long as enable_custom_prng is False. The way we handle this is
# that `random.split` adapts the input/output by converting to/from
# key arrays across its call to `random_split`. So we rely on these
# wrap/unwrap casting primitives to allow that conversion under jit.
#
# We may want to keep both around for testing and debugging escape
# hatches. We can rename them `unsafe` for emphasis, and/or issue a
# warning on entry to the traceable.
#
# TODO(frostig): Consider removal once we always enable_custom_prng.

def random_wrap(base_arr, *, impl):
  _check_prng_key_data(impl, base_arr)
  return random_wrap_p.bind(base_arr, impl=impl)

random_wrap_p = core.Primitive('random_wrap')
ad.defjvp_zero(random_wrap_p)

@random_wrap_p.def_abstract_eval
def random_wrap_abstract_eval(base_arr_aval, *, impl):
  shape = base_arr_shape_to_keys_shape(impl, base_arr_aval.shape)
  return keys_shaped_array(impl, shape)

@random_wrap_p.def_impl
def random_wrap_impl(base_arr, *, impl):
  return PRNGKeyArray(impl, base_arr)

def random_wrap_lowering(ctx, base_arr, *, impl):
  return [base_arr]

def random_wrap_batch_rule(batched_args, batch_dims, *, impl):
  x, = batched_args
  d, = batch_dims
  x = batching.bdim_at_front(x, d, 1)
  return random_wrap(x, impl=impl), 0

mlir.register_lowering(random_wrap_p, random_wrap_lowering)
batching.primitive_batchers[random_wrap_p] = random_wrap_batch_rule


def random_unwrap(keys):
  assert isinstance(keys, PRNGKeyArray)
  return random_unwrap_p.bind(keys)

random_unwrap_p = core.Primitive('random_unwrap')
ad.defjvp_zero(random_unwrap_p)
batching.defvectorized(random_unwrap_p)

@random_unwrap_p.def_abstract_eval
def random_unwrap_abstract_eval(keys_aval):
  return keys_aval_to_base_arr_aval(keys_aval)

@random_unwrap_p.def_impl
def random_unwrap_impl(keys):
  return keys.unsafe_raw_array()

def random_unwrap_lowering(ctx, keys):
  return [keys]

mlir.register_lowering(random_unwrap_p, random_unwrap_lowering)


# -- threefry2x32 PRNG implementation


def _is_threefry_prng_key(key: jnp.ndarray) -> bool:
  try:
    return key.shape == (2,) and key.dtype == np.uint32
  except AttributeError:
    return False


def threefry_seed(seed: jnp.ndarray) -> jnp.ndarray:
  """Create a single raw threefry PRNG key from an integer seed.

  Args:
    seed: a 64- or 32-bit integer used as the value of the key.

  Returns:
    The PRNG key contents, modeled as an array of shape (2,) and dtype
    uint32. The key is constructed from a 64-bit seed by effectively
    bit-casting to a pair of uint32 values (or from a 32-bit seed by
    first padding out with zeros).
  """
  if seed.shape:
    raise TypeError(f"PRNG key seed must be a scalar; got {seed!r}.")
  if not np.issubdtype(seed.dtype, np.integer):
    raise TypeError(f"PRNG key seed must be an integer; got {seed!r}")
  convert = lambda k: lax.reshape(lax.convert_element_type(k, np.uint32), [1])
  k1 = convert(
      lax.shift_right_logical(seed, lax_internal._const(seed, 32)))
  with jax.numpy_dtype_promotion('standard'):
    # TODO(jakevdp): in X64 mode, this can generate 64-bit computations for 32-bit
    # inputs. We should avoid this.
    k2 = convert(jnp.bitwise_and(seed, np.uint32(0xFFFFFFFF)))
  return lax.concatenate([k1, k2], 0)


def _make_rotate_left(dtype):
  if not jnp.issubdtype(dtype, np.integer):
    raise TypeError("_rotate_left only accepts integer dtypes.")
  nbits = np.array(jnp.iinfo(dtype).bits, dtype)

  def _rotate_left(x, d):
    if lax.dtype(d) != dtype:
      d = lax.convert_element_type(d, dtype)
    if lax.dtype(x) != dtype:
      x = lax.convert_element_type(x, dtype)
    return lax.shift_left(x, d) | lax.shift_right_logical(x, nbits - d)
  return _rotate_left


def _bit_stats(bits):
  """This is a debugging function to compute the statistics of bit fields."""
  return np.array([list(map(int, np.binary_repr(x, 64))) for x in bits]).mean(0)


### hash function and split

def _threefry2x32_abstract_eval(*args):
  if any(a.dtype != jnp.uint32 for a in args):
    raise TypeError("Arguments to threefry2x32 must have uint32 type, got {}"
                    .format(args))
  if all(isinstance(arg, core.ShapedArray) for arg in args):
    shape = lax_internal.broadcasting_shape_rule(*args)
    named_shape = core.join_named_shapes(*(a.named_shape for a in args))
    aval = core.ShapedArray(shape, jnp.dtype(jnp.uint32), named_shape=named_shape)
  else:
    aval = core.UnshapedArray(jnp.dtype(jnp.uint32))
  return (aval,) * 2


rotate_left = _make_rotate_left(np.uint32)


def apply_round(v, rot):
  v = v[:]
  v[0] = v[0] + v[1]
  v[1] = rotate_left(v[1], rot)
  v[1] = v[0] ^ v[1]
  return v


def rotate_list(xs):
  return xs[1:] + xs[:1]


def rolled_loop_step(i, state):
  x, ks, rotations = state
  for r in rotations[0]:
    x = apply_round(x, r)
  new_x = [x[0] + ks[0], x[1] + ks[1] + jnp.asarray(i + 1, dtype=np.uint32)]
  return new_x, rotate_list(ks), rotate_list(rotations)


def _threefry2x32_lowering(key1, key2, x1, x2, use_rolled_loops=True):
  """Apply the Threefry 2x32 hash.

  Args:
    keypair: a pair of 32bit unsigned integers used for the key.
    count: an array of dtype uint32 used for the counts.

  Returns:
    An array of dtype uint32 with the same shape as `count`.
  """
  x = [x1, x2]

  rotations = [np.array([13, 15, 26, 6], dtype=np.uint32),
               np.array([17, 29, 16, 24], dtype=np.uint32)]
  ks = [key1, key2, key1 ^ key2 ^ np.uint32(0x1BD11BDA)]

  x[0] = x[0] + ks[0]
  x[1] = x[1] + ks[1]

  if use_rolled_loops:
    x, _, _ = lax.fori_loop(0, 5, rolled_loop_step, (x, rotate_list(ks), rotations))

  else:
    for r in rotations[0]:
      x = apply_round(x, r)
    x[0] = x[0] + ks[1]
    x[1] = x[1] + ks[2] + np.uint32(1)

    for r in rotations[1]:
      x = apply_round(x, r)
    x[0] = x[0] + ks[2]
    x[1] = x[1] + ks[0] + np.uint32(2)

    for r in rotations[0]:
      x = apply_round(x, r)
    x[0] = x[0] + ks[0]
    x[1] = x[1] + ks[1] + np.uint32(3)

    for r in rotations[1]:
      x = apply_round(x, r)
    x[0] = x[0] + ks[1]
    x[1] = x[1] + ks[2] + np.uint32(4)

    for r in rotations[0]:
      x = apply_round(x, r)
    x[0] = x[0] + ks[2]
    x[1] = x[1] + ks[0] + np.uint32(5)

  return tuple(x)


def _threefry2x32_gpu_lowering(threefry2x32_lowering, ctx, k1, k2, x1, x2):
  aval_out, _ = ctx.avals_out
  k1_aval, k2_aval, x1_aval, x2_aval = ctx.avals_in
  rank = len(aval_out.shape)
  if 0 in aval_out.shape:
    zeros = mlir.full_like_aval(0, aval_out)
    return [zeros, zeros]
  def _broadcast(x, aval):
    return mhlo.BroadcastInDimOp(
        mlir.aval_to_ir_type(aval_out), x,
        mlir.dense_int_elements(range(rank - len(aval.shape), rank))).result
  return threefry2x32_lowering(
          (_broadcast(k1, k1_aval), _broadcast(k2, k2_aval)),
          (_broadcast(x1, x1_aval), _broadcast(x2, x2_aval)))


threefry2x32_p = core.Primitive("threefry2x32")
threefry2x32_p.multiple_results = True
threefry2x32_p.def_impl(partial(xla.apply_primitive, threefry2x32_p))
threefry2x32_p.def_abstract_eval(_threefry2x32_abstract_eval)
batching.defbroadcasting(threefry2x32_p)
mlir.register_lowering(threefry2x32_p, mlir.lower_fun(
    partial(_threefry2x32_lowering, use_rolled_loops=False),
    multiple_results=True))
mlir.register_lowering(threefry2x32_p, mlir.lower_fun(
    partial(_threefry2x32_lowering, use_rolled_loops=True),
    multiple_results=True), platform='cpu')
mlir.register_lowering(
    threefry2x32_p,
    partial(_threefry2x32_gpu_lowering, gpu_prng.cuda_threefry2x32),
    platform='cuda')
mlir.register_lowering(
    threefry2x32_p,
    partial(_threefry2x32_gpu_lowering, gpu_prng.rocm_threefry2x32),
    platform='rocm')


@partial(jit, inline=True)
def threefry_2x32(keypair, count):
  """Apply the Threefry 2x32 hash.

  Args:
    keypair: a pair of 32bit unsigned integers used for the key.
    count: an array of dtype uint32 used for the counts.

  Returns:
    An array of dtype uint32 with the same shape as `count`.
  """
  key1, key2 = keypair
  if not lax.dtype(key1) == lax.dtype(key2) == lax.dtype(count) == np.uint32:
    msg = "threefry_2x32 requires uint32 arguments, got {}"
    raise TypeError(msg.format([lax.dtype(x) for x in [key1, key2, count]]))

  try:
    odd_size = count.size % 2
  except core.InconclusiveDimensionOperation as e:
    msg = ("jax.random functions have limited support for shape polymorphism. "
           "In particular, the product of the known dimensions must be even.")
    raise core.InconclusiveDimensionOperation(msg) from e

  if odd_size:
    x = list(jnp.split(jnp.concatenate([count.ravel(), np.uint32([0])]), 2))
  else:
    x = list(jnp.split(count.ravel(), 2))

  x = threefry2x32_p.bind(key1, key2, x[0], x[1])
  out = jnp.concatenate(x)
  assert out.dtype == np.uint32
  return lax.reshape(out[:-1] if odd_size else out, count.shape)


def threefry_split(key: jnp.ndarray, num: int) -> jnp.ndarray:
  return _threefry_split(key, int(num))  # type: ignore

@partial(jit, static_argnums=(1,), inline=True)
def _threefry_split(key, num) -> jnp.ndarray:
  counts = lax.iota(np.uint32, num * 2)
  return lax.reshape(threefry_2x32(key, counts), (num, 2))


def threefry_fold_in(key: jnp.ndarray, data: jnp.ndarray) -> jnp.ndarray:
  assert not data.shape
  return _threefry_fold_in(key, jnp.uint32(data))

@partial(jit, inline=True)
def _threefry_fold_in(key, data):
  return threefry_2x32(key, threefry_seed(data))


@partial(jit, static_argnums=(1, 2), inline=True)
def threefry_random_bits(key: jnp.ndarray, bit_width, shape):
  """Sample uniform random bits of given width and shape using PRNG key."""
  if not _is_threefry_prng_key(key):
    raise TypeError("threefry_random_bits got invalid prng key.")
  if bit_width not in (8, 16, 32, 64):
    raise TypeError("requires 8-, 16-, 32- or 64-bit field width.")
  size = prod(shape)
  # Compute ceil(bit_width * size / 32) in a way that is friendly to shape
  # polymorphism
  max_count, r = divmod(bit_width * size, 32)
  if r > 0:
    max_count += 1

  if core.is_constant_dim(max_count):
    nblocks, rem = divmod(max_count, jnp.iinfo(np.uint32).max)
  else:
    nblocks, rem = 0, max_count

  if not nblocks:
    bits = threefry_2x32(key, lax.iota(np.uint32, rem))
  else:
    keys = threefry_split(key, nblocks + 1)
    subkeys, last_key = keys[:-1], keys[-1]
    blocks = vmap(threefry_2x32, in_axes=(0, None))(subkeys, lax.iota(np.uint32, jnp.iinfo(np.uint32).max))
    last = threefry_2x32(last_key, lax.iota(np.uint32, rem))
    bits = lax.concatenate([blocks.ravel(), last], 0)

  dtype = UINT_DTYPES[bit_width]
  if bit_width == 64:
    bits = [lax.convert_element_type(x, dtype) for x in jnp.split(bits, 2)]
    bits = lax.shift_left(bits[0], dtype(32)) | bits[1]
  elif bit_width in [8, 16]:
    # this is essentially bits.view(dtype)[:size]
    bits = lax.bitwise_and(
      np.uint32(np.iinfo(dtype).max),
      lax.shift_right_logical(
        lax.broadcast(bits, (1,)),
        lax.mul(
          np.uint32(bit_width),
          lax.broadcasted_iota(np.uint32, (32 // bit_width, 1), 0)
        )
      )
    )
    bits = lax.reshape(bits, ((max_count * 32 // bit_width),), (1, 0))
    bits = lax.convert_element_type(bits, dtype)[:size]
  return lax.reshape(bits, shape)


threefry_prng_impl = PRNGImpl(
    key_shape=(2,),
    seed=threefry_seed,
    split=threefry_split,
    random_bits=threefry_random_bits,
    fold_in=threefry_fold_in,
    tag='fry')


# -- RngBitGenerator PRNG implementation

# This code is experimental!
# https://www.tensorflow.org/xla/operation_semantics#rngbitgenerator
# Notice that the RngBitGenerator operations are not guaranteed to be
# stable/deterministic across backends or compiler versions. Correspondingly, we
# reserve the right to change any of these implementations at any time!

def _rbg_seed(seed: jnp.ndarray) -> jnp.ndarray:
  assert not seed.shape
  halfkey = threefry_seed(seed)
  return jnp.concatenate([halfkey, halfkey])

def _rbg_split(key: jnp.ndarray, num: int) -> jnp.ndarray:
  return vmap(_threefry_split, (0, None), 1)(key.reshape(2, 2), num).reshape(num, 4)

def _rbg_fold_in(key: jnp.ndarray, data: jnp.ndarray) -> jnp.ndarray:
  assert not data.shape
  return vmap(_threefry_fold_in, (0, None), 0)(key.reshape(2, 2), data).reshape(4)

def _rbg_random_bits(key: jnp.ndarray, bit_width: int, shape: Sequence[int]
                     ) -> jnp.ndarray:
  if not key.shape == (4,) and key.dtype == jnp.dtype('uint32'):
    raise TypeError("_rbg_random_bits got invalid prng key.")
  if bit_width not in (8, 16, 32, 64):
    raise TypeError("requires 8-, 16-, 32- or 64-bit field width.")
  _, bits = lax.rng_bit_generator(key, shape, dtype=UINT_DTYPES[bit_width])
  return bits

rbg_prng_impl = PRNGImpl(
    key_shape=(4,),
    seed=_rbg_seed,
    split=_rbg_split,
    random_bits=_rbg_random_bits,
    fold_in=_rbg_fold_in,
    tag='rbg')

def _unsafe_rbg_split(key: jnp.ndarray, num: int) -> jnp.ndarray:
  # treat 10 iterations of random bits as a 'hash function'
  _, keys = lax.rng_bit_generator(key, (10 * num, 4), dtype='uint32')
  return keys[::10]

def _unsafe_rbg_fold_in(key: jnp.ndarray, data: jnp.ndarray) -> jnp.ndarray:
  assert not data.shape
  _, random_bits = lax.rng_bit_generator(_rbg_seed(data), (10, 4), dtype='uint32')
  return key ^ random_bits[-1]

unsafe_rbg_prng_impl = PRNGImpl(
    key_shape=(4,),
    seed=_rbg_seed,
    split=_unsafe_rbg_split,
    random_bits=_rbg_random_bits,
    fold_in=_unsafe_rbg_fold_in,
    tag='urbg')
