# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Script Language Operators."""

# pylint: disable=g-bad-name
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import threading

# Used by py_util.cc to get tracebacks.
import traceback  # pylint: disable=unused-import
import weakref

import numpy as np
import six

from tensorflow.python.eager import backprop
from tensorflow.python.eager import context
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import func_graph
from tensorflow.python.framework import function
from tensorflow.python.framework import ops
from tensorflow.python.lib.core import _pywrap_py_func
from tensorflow.python.ops import gen_script_ops
from tensorflow.python.ops import resource_variable_ops
from tensorflow.python.util import compat
from tensorflow.python.util import deprecation
from tensorflow.python.util import dispatch
from tensorflow.python.util import lazy_loader
from tensorflow.python.util import nest
from tensorflow.python.util import tf_inspect
from tensorflow.python.util.tf_export import tf_export

autograph = lazy_loader.LazyLoader(
    "autograph", globals(),
    "tensorflow.python.autograph.impl.api")


# Map from EagerPyFunc token to tuple (tape, eager args, eager outputs);
# used for differentiation.
tape_cache = {}


def _maybe_copy_to_context_device(tensor, device_name):
  """Copy an EagerTensor to the current device if it's not on `device_name`."""
  in_device = tensor.backing_device
  if device_name == in_device:
    return tensor
  else:
    # Note that EagerTensor._copy bypasses the placer and copies to the context
    # device, which means e.g. int32 Tensors which would normally be forced onto
    # the CPU can instead be placed on the GPU. This is necessary so that the
    # PyFunc kernel always returns Tensors on the device it's executing on.
    return tensor._copy()  # pylint: disable=protected-access


class EagerFunc(object):
  """A wrapper for a function owned by an EagerPyFunc."""

  def __init__(self, func, Tout, is_grad_func, use_tape_cache=True):
    """Constructs an EagerFunc.

    Args:
      func: The function to wrap.
      Tout: A list of datatypes for the output; an empty list if the output is
        None.
      is_grad_func: Whether this EagerFunc is the gradient of another
        EagerPyFunc.
      use_tape_cache: (Optional.) Whether to cache `func` in the `tape_cache`.
        For additional information, see description of `_eager_py_func`.
        This parameter should be removed once the #35084 issue is fixed.
    """
    self._func = func
    self._out_dtypes = Tout
    self._is_grad_func = is_grad_func
    self._use_tape_cache = use_tape_cache

  def _convert(self, value, dtype):
    """Converts `value` to a tensor of type `dtype`, with error checking.

    Args:
      value: The tensor to convert.
      dtype: The desired dtype.

    Returns:
      A tensor of type `dtype`, or a zeros tensor if value is None and
      this function is in fact a gradient function.

    Raises:
      RuntimeError: if `value` is a variable.
    """

    if isinstance(value, resource_variable_ops.ResourceVariable):
      raise RuntimeError(
          "Attempting to return a variable from an eagerly executed py_func. "
          "Only numeric data structures like Tensors or NumPy arrays should "
          "be returned; to return the value of a variable, make sure to obtain "
          "the Tensor backing it by calling `.read_value()` on the variable in "
          f"question: {value}")
    if value is None and self._is_grad_func:
      # Gradient functions may legitimately return a list that contains
      # both Tensors and Python Nones. Unfortunately this breaks the
      # OpKernel, so for now we replace None objects with zeros, which is
      # mathematically correct but will prevent short-circuiting gradient
      # computations.
      #
      # TODO(akshayka): Make it possible to return a list of both Tensors and
      # Nones from an EagerPyFunc.
      return constant_op.constant(0.0, dtype=dtype)
    return ops.convert_to_tensor(value, dtype=dtype)

  def __call__(self, device, token, args):
    """Passes `args` to `self._func`, which is executed eagerly."""

    with context.eager_mode(), backprop.GradientTape() as tape:
      # Only watch tensors with a floating or complex dtype.
      for tensor in args:
        for t in nest.flatten(tensor):
          if t.dtype.is_floating or t.dtype.is_complex:
            tape.watch(t)
      ret = self._func(*args)
      # copy the returned tensors to the PyFunc op's device if necessary.
      device_name = device
      if device_name is None:
        # "None" here means "CPU", from the nullptr convention with C++ device
        # pointers.
        device_name = "/job:localhost/replica:0/task:0/device:CPU:0"
      with ops.device(device):
        if isinstance(ret, (tuple, list)):
          outputs = [
              _maybe_copy_to_context_device(self._convert(x, dtype=dtype),
                                            device_name)
              for (x, dtype) in zip(ret, self._out_dtypes)
          ]
        elif ret is None:
          outputs = None
        else:
          outputs = _maybe_copy_to_context_device(
              self._convert(ret, dtype=self._out_dtypes[0]), device_name)
    if self._use_tape_cache:
      tape_cache[compat.as_bytes(token)] = (tape, args, outputs)
    return outputs


class FuncRegistry(object):
  """A helper class to keep track of registered py functions.

  FuncRegistry keeps a map from unique tokens (string) to python
  functions, which takes numpy arrays and outputs numpy arrays.
  """

  def __init__(self):
    self._lock = threading.Lock()
    self._unique_id = 0  # GUARDED_BY(self._lock)
    # Only store weakrefs to the functions. The strong reference is stored in
    # the graph.
    self._funcs = weakref.WeakValueDictionary()

  @property
  def _ctx(self):
    # N.B. This is needed to support calling py_func with GPU tensors,
    # which must be transferred to CPU if used in any of the NumPy APIs.
    context.ensure_initialized()
    return context.context()._handle  # pylint: disable=protected-access

  def insert(self, func):
    """Registers `func` and returns a unique token for this entry."""
    token = self._next_unique_token()
    # Store a weakref to the function
    self._funcs[token] = func
    return token

  def remove(self, token):
    """Removes the registered function corresponding to `token`."""
    self._funcs.pop(token, None)

  @staticmethod
  def _convert(value, dtype=None):
    """Converts an arg to numpy, avoiding dangerous string and unicode dtypes.

    Numpy pads with zeros when using string and unicode dtypes if different
    components of a tensor have different lengths.  This is bad: ignoring the
    padding is wrong for text data, and removing the padding is wrong for binary
    data.  To avoid this bug, we redo the conversion using an object dtype.
    Additionally, we convert unicode strings to (byte-)strings for
    compatibility.

    Args:
      value: Value to convert to a numpy array.
      dtype: (Optional.) Desired NumPy type for the returned value.

    Returns:
      A numpy array.
    """
    result = np.asarray(value, dtype=dtype, order="C")
    if result.dtype.char == "S" and result is not value:
      return np.asarray(value, order="C", dtype=object)
    elif result.dtype.char == "U" and result is not value:
      value = np.vectorize(lambda x: x.encode("utf8"))(value)
      return np.asarray(value, order="C", dtype=object)
    elif result.dtype.char == "U":
      return result.astype(np.bytes_)
    else:
      return result

  def __call__(self, token, device, args):
    """Calls the registered function for `token` with args.

    Args:
      token: A key into this `FuncRegistry` identifying which function to call.
      device: Name of the device on which outputs of `token`'s corresponding
        operation should be placed. Used iff the function registered for `token`
        is an EagerPyFunc.
      args: The arguments to pass to the function registered for `token`.

    Returns:
      The output of the function registered for `token`.

    Raises:
      ValueError: if no function is registered for `token`.
    """
    func = self._funcs.get(token, None)
    if func is None:
      raise ValueError(f"Could not find callback with key={token} in the "
                       "registry.")
    if isinstance(func, EagerFunc):
      # NB: Different invocations of the same py_func will share the same
      # token, and the entries they stash in the tape_cache will collide.
      # In practice, when executing a graph, this should only happen if
      # the py_func is in a while_loop whose iterations are run in parallel
      # or if the graph is being driven by concurrent session.run() calls.
      #
      # TODO(akshayka): Key the tape cache in a thread-safe way.
      return func(device, token, args)
    else:
      ret = func(*args)
      # Strings seem to lead to a memory leak here if they're not wrapped in a
      # list.
      if isinstance(ret, six.binary_type):
        ret = [ret]
      # Ensures that we return either a single numpy array or a list of numpy
      # arrays.
      if isinstance(ret, (tuple, list)):
        return [self._convert(x) for x in ret]
      else:
        return self._convert(ret)

  def size(self):
    """Returns how many functions are currently registered."""
    return len(self._funcs)

  def _next_unique_token(self):
    """Returns a unique token."""
    with self._lock:
      uid = self._unique_id
      self._unique_id += 1
    return "pyfunc_%d" % uid


# Global registry for py functions.
_py_funcs = FuncRegistry()

_pywrap_py_func.initialize_py_trampoline(_py_funcs)


def _internal_py_func(func,
                      inp,
                      Tout,
                      stateful=None,
                      eager=False,
                      is_grad_func=False,
                      name=None,
                      use_tape_cache=True):
  """See documentation for py_func and eager_py_func."""
  if not callable(func):
    raise ValueError(
        f"Expected func to be callable. Received func={func} of type "
        f"{type(func)}.")

  original_func = func
  func = autograph.do_not_convert(func)

  is_list_or_tuple = False
  if isinstance(Tout, (list, tuple)):
    is_list_or_tuple = True
  else:
    Tout = [Tout]

  if eager:
    func = EagerFunc(func, Tout, is_grad_func, use_tape_cache=use_tape_cache)

  # Tying the registered function's lifetime with the current default graph is
  # not reliable. For example, Estimator-based binaries may switch graphs in
  # between model training end evaluation, via saved_model. Those binaries work
  # because the original function is global, and break once the registered
  # function is an anonymous lambda, like the one produced by do_not_convert.
  # To avoid breaking those cases, we attach the wrapper to the original
  # function so that their lifetime is connected.
  # TODO(b/144286616): Remove this.
  if tf_inspect.isfunction(original_func):
    # Note: this check is needed because original_func may be a descriptor
    # (https://docs.python.org/3/howto/descriptor.html)
    # and we can't attach attributes to those.
    original_func.ag_dnc_wrapper__ = func

  token = _py_funcs.insert(func)
  # We tie the registered function's lifetime with the current default graph,
  # i.e., when the current graph is destroyed, we remove its py funcs.
  graph = ops.get_default_graph()

  while True:
    current_graph = graph
    if isinstance(graph, function._FuncGraph):  # pylint: disable=protected-access
      graph = graph._outer_graph  # pylint: disable=protected-access
    elif isinstance(graph, func_graph.FuncGraph):
      graph = graph.outer_graph
    if graph is current_graph:
      break

  # TODO(zhifengc): Consider adding a Graph method to collect
  # `cleanup` objects in one of its member.
  if not hasattr(graph, "_py_funcs_used_in_graph"):
    graph._py_funcs_used_in_graph = []  # pylint: disable=protected-access

  # Store a reference to the function in the graph to ensure it stays alive
  # as long as the graph lives. When the graph is destroyed, the function
  # is left to the garbage collector for destruction as well.
  graph._py_funcs_used_in_graph.append(func)  # pylint: disable=protected-access

  if eager:
    result = gen_script_ops.eager_py_func(
        input=inp,
        token=token,
        is_async=context.is_async(),
        Tout=Tout,
        name=name)
  else:
    if stateful:
      result = gen_script_ops.py_func(
          input=inp, token=token, Tout=Tout, name=name)
    else:
      result = gen_script_ops.py_func_stateless(
          input=inp, token=token, Tout=Tout, name=name)
  return result if is_list_or_tuple else result[0]


# TODO(akshayka): Implement higher-order derivatives.
@ops.RegisterGradient("EagerPyFunc")
def _EagerPyFuncGrad(op, *dy):
  """Computes the gradient of an EagerPyFunc."""

  token = op.get_attr("token")

  def eagerly_executed_grad(*dy):
    tape, eager_inputs, eager_outputs = tape_cache.pop(compat.as_bytes(token))
    return tape.gradient(eager_outputs, eager_inputs, output_gradients=dy)

  with ops.control_dependencies(op.outputs):
    return _internal_py_func(
        func=eagerly_executed_grad,
        inp=dy,
        Tout=[tensor.dtype for tensor in op.inputs],
        eager=True,
        is_grad_func=True)


def _eager_py_func(func, inp, Tout, name=None, use_tape_cache=True):
  """Wraps a python function into a TensorFlow op that executes it eagerly.

  This function is the internal implementation for `eager_py_func`, see the
  `eager_py_func` docstring for the full description.

  Note: this function as a layer of indirection was added with one
  specific purpose: as a workaround for github issue #35084.
  It does all the same as `eager_py_func` used to do with one difference:
  it can be used to instruct underlying EagerFunc not to use `tape_cache`
  to avoid memory leak. When the issue #35084 is fixed - this function should
  be removed, its body should be moved back to become the body of
  `eager_py_func` and all the call sites should be reverted to
  using `eager_py_func` without `use_tape_cache` argument of any value.

  Args:
    func: A Python function which accepts a list of `Tensor` objects having
      element types that match the corresponding `tf.Tensor` objects in `inp`
      and returns a list of `Tensor` objects (or a single `Tensor`, or `None`)
      having element types that match the corresponding values in `Tout`.
    inp: A list of `Tensor` objects.
    Tout: A list or tuple of tensorflow data types or a single tensorflow data
      type if there is only one, indicating what `func` returns; an empty list
      if no value is returned (i.e., if the return value is `None`).
    name: A name for the operation (optional).
    use_tape_cache: (Optional.) Whether to cache `func` in the `tape_cache`.
      For additional information, see description of `_eager_py_func`.
      This parameter should be removed once the #35084 issue is fixed.

  Returns:
    A list of `Tensor` or a single `Tensor` which `func` computes; an empty list
    if `func` returns None.
  """
  if ops.executing_eagerly_outside_functions():
    with ops.device(context.context().host_address_space()):
      return _internal_py_func(
          func=func,
          inp=inp,
          Tout=Tout,
          eager=True,
          name=name,
          use_tape_cache=use_tape_cache)

  return _internal_py_func(
      func=func,
      inp=inp,
      Tout=Tout,
      eager=True,
      name=name,
      use_tape_cache=use_tape_cache)


@tf_export("py_function")
@dispatch.add_dispatch_support
def eager_py_func(func, inp, Tout, name=None):
  """Wraps a python function into a TensorFlow op that executes it eagerly.

  This function allows expressing computations in a TensorFlow graph as
  Python functions. In particular, it wraps a Python function `func`
  in a once-differentiable TensorFlow operation that executes it with eager
  execution enabled. As a consequence, `tf.py_function` makes it
  possible to express control flow using Python constructs (`if`, `while`,
  `for`, etc.), instead of TensorFlow control flow constructs (`tf.cond`,
  `tf.while_loop`). For example, you might use `tf.py_function` to
  implement the log huber function:

  ```python
  def log_huber(x, m):
    if tf.abs(x) <= m:
      return x**2
    else:
      return m**2 * (1 - 2 * tf.math.log(m) + tf.math.log(x**2))

  x = tf.compat.v1.placeholder(tf.float32)
  m = tf.compat.v1.placeholder(tf.float32)

  y = tf.py_function(func=log_huber, inp=[x, m], Tout=tf.float32)
  dy_dx = tf.gradients(y, x)[0]

  with tf.compat.v1.Session() as sess:
    # The session executes `log_huber` eagerly. Given the feed values below,
    # it will take the first branch, so `y` evaluates to 1.0 and
    # `dy_dx` evaluates to 2.0.
    y, dy_dx = sess.run([y, dy_dx], feed_dict={x: 1.0, m: 2.0})
  ```

  You can also use `tf.py_function` to debug your models at runtime
  using Python tools, i.e., you can isolate portions of your code that
  you want to debug, wrap them in Python functions and insert `pdb` tracepoints
  or print statements as desired, and wrap those functions in
  `tf.py_function`.

  For more information on eager execution, see the
  [Eager guide](https://tensorflow.org/guide/eager).

  `tf.py_function` is similar in spirit to `tf.compat.v1.py_func`, but unlike
  the latter, the former lets you use TensorFlow operations in the wrapped
  Python function. In particular, while `tf.compat.v1.py_func` only runs on CPUs
  and
  wraps functions that take NumPy arrays as inputs and return NumPy arrays as
  outputs, `tf.py_function` can be placed on GPUs and wraps functions
  that take Tensors as inputs, execute TensorFlow operations in their bodies,
  and return Tensors as outputs.

  Like `tf.compat.v1.py_func`, `tf.py_function` has the following limitations
  with respect to serialization and distribution:

  * The body of the function (i.e. `func`) will not be serialized in a
    `GraphDef`. Therefore, you should not use this function if you need to
    serialize your model and restore it in a different environment.

  * The operation must run in the same address space as the Python program
    that calls `tf.py_function()`. If you are using distributed
    TensorFlow, you must run a `tf.distribute.Server` in the same process as the
    program that calls `tf.py_function()` and you must pin the created
    operation to a device in that server (e.g. using `with tf.device():`).


  Args:
    func: A Python function which accepts a list of `Tensor` objects having
      element types that match the corresponding `tf.Tensor` objects in `inp`
      and returns a list of `Tensor` objects (or a single `Tensor`, or `None`)
      having element types that match the corresponding values in `Tout`.
    inp: A list of `Tensor` objects.
    Tout: A list or tuple of tensorflow data types or a single tensorflow data
      type if there is only one, indicating what `func` returns; an empty list
      if no value is returned (i.e., if the return value is `None`).
    name: A name for the operation (optional).

  Returns:
    A list of `Tensor` or a single `Tensor` which `func` computes; an empty list
    if `func` returns None.
  """
  return _eager_py_func(
      func=func, inp=inp, Tout=Tout, name=name, use_tape_cache=True)


def py_func_common(func, inp, Tout, stateful=True, name=None):
  """Wraps a python function and uses it as a TensorFlow op.

  Given a python function `func`, which takes numpy arrays as its
  arguments and returns numpy arrays as its outputs, wrap this function as an
  operation in a TensorFlow graph. The following snippet constructs a simple
  TensorFlow graph that invokes the `np.sinh()` NumPy function as a operation
  in the graph:

  ```python
  def my_func(x):
    # x will be a numpy array with the contents of the placeholder below
    return np.sinh(x)
  input = tf.compat.v1.placeholder(tf.float32)
  y = tf.compat.v1.py_func(my_func, [input], tf.float32)
  ```

  **N.B.** The `tf.compat.v1.py_func()` operation has the following known
  limitations:

  * The body of the function (i.e. `func`) will not be serialized in a
    `GraphDef`. Therefore, you should not use this function if you need to
    serialize your model and restore it in a different environment.

  * The operation must run in the same address space as the Python program
    that calls `tf.compat.v1.py_func()`. If you are using distributed
    TensorFlow, you
    must run a `tf.distribute.Server` in the same process as the program that
    calls
    `tf.compat.v1.py_func()` and you must pin the created operation to a device
    in that
    server (e.g. using `with tf.device():`).
    
  Note: It produces tensors of unknown shape and rank as shape inference 
    does not work on arbitrary Python code.
    If you need the shape, you need to set it based on statically 
    available information.
    
    E.g.
    ```python
    import tensorflow as tf
    import numpy as np

    def make_synthetic_data(i):
        return np.cast[np.uint8](i) * np.ones([20,256,256,3],
                dtype=np.float32) / 10.

    def preprocess_fn(i):
        ones = tf.py_function(make_synthetic_data,[i],tf.float32)
        ones.set_shape(tf.TensorShape([None, None, None, None]))
        ones = tf.image.resize(ones, [224,224])
        return ones

    ds = tf.data.Dataset.range(10)
    ds = ds.map(preprocess_fn)
    ```
    
  Args:
    func: A Python function, which accepts `ndarray` objects as arguments and
      returns a list of `ndarray` objects (or a single `ndarray`). This function
      must accept as many arguments as there are tensors in `inp`, and these
      argument types will match the corresponding `tf.Tensor` objects in `inp`.
      The returns `ndarray`s must match the number and types defined `Tout`.
      Important Note: Input and output numpy `ndarray`s of `func` are not
        guaranteed to be copies. In some cases their underlying memory will be
        shared with the corresponding TensorFlow tensors. In-place modification
        or storing `func` input or return values in python datastructures
        without explicit (np.)copy can have non-deterministic consequences.
    inp: A list of `Tensor` objects.
    Tout: A list or tuple of tensorflow data types or a single tensorflow data
      type if there is only one, indicating what `func` returns.
    stateful: (Boolean.) If True, the function should be considered stateful. If
      a function is stateless, when given the same input it will return the same
      output and have no observable side effects. Optimizations such as common
      subexpression elimination are only performed on stateless operations.
    name: A name for the operation (optional).

  Returns:
    A list of `Tensor` or a single `Tensor` which `func` computes.

  @compatibility(TF2)

  This name was deprecated and removed in TF2, but `tf.numpy_function` is a
  near-exact replacement, just drop the `stateful` argument (all
  `tf.numpy_function` calls are considered stateful). It is compatible with
  eager execution and `tf.function`.

  `tf.py_function` is a close but not an exact replacement, passing TensorFlow
  tensors to the wrapped function instead of NumPy arrays, which provides
  gradients and can take advantage of accelerators.

  Before:

  >>> def fn_using_numpy(x):
  ...   x[0] = 0.
  ...   return x
  >>> tf.compat.v1.py_func(fn_using_numpy, inp=[tf.constant([1., 2.])],
  ...     Tout=tf.float32, stateful=False)
  <tf.Tensor: shape=(2,), dtype=float32, numpy=array([0., 2.], dtype=float32)>

  After:

  >>> tf.numpy_function(fn_using_numpy, inp=[tf.constant([1., 2.])],
  ...     Tout=tf.float32)
  <tf.Tensor: shape=(2,), dtype=float32, numpy=array([0., 2.], dtype=float32)>

  @end_compatibility

  """
  if context.executing_eagerly():
    result = func(*[np.array(x) for x in inp])
    result = nest.flatten(result)

    result = [x if x is None else ops.convert_to_tensor(x) for x in result]
    if len(result) == 1:
      # Mimic the automatic unwrapping in graph-mode py_func
      result, = result
    return result

  if ops.executing_eagerly_outside_functions():
    with ops.device(context.context().host_address_space()):
      return _internal_py_func(
          func=func,
          inp=inp,
          Tout=Tout,
          stateful=stateful,
          eager=False,
          name=name)

  return _internal_py_func(
      func=func, inp=inp, Tout=Tout, stateful=stateful, eager=False, name=name)


@deprecation.deprecated(
    date=None,
    instructions="""tf.py_func is deprecated in TF V2. Instead, there are two
    options available in V2.
    - tf.py_function takes a python function which manipulates tf eager
    tensors instead of numpy arrays. It's easy to convert a tf eager tensor to
    an ndarray (just call tensor.numpy()) but having access to eager tensors
    means `tf.py_function`s can use accelerators such as GPUs as well as
    being differentiable using a gradient tape.
    - tf.numpy_function maintains the semantics of the deprecated tf.py_func
    (it is not differentiable, and manipulates numpy arrays). It drops the
    stateful argument making all functions stateful.
    """)
@tf_export(v1=["py_func"])
@dispatch.add_dispatch_support
def py_func(func, inp, Tout, stateful=True, name=None):
  return py_func_common(func, inp, Tout, stateful, name=name)


py_func.__doc__ = "%s" % py_func_common.__doc__


@tf_export("numpy_function")
@dispatch.add_dispatch_support
def numpy_function(func, inp, Tout, name=None):
  """Wraps a python function and uses it as a TensorFlow op.

  Given a python function `func` wrap this function as an operation in a
  TensorFlow function. `func` must take numpy arrays as its arguments and
  return numpy arrays as its outputs.

  The following example creates a TensorFlow graph with `np.sinh()` as an
  operation in the graph:

  >>> def my_numpy_func(x):
  ...   # x will be a numpy array with the contents of the input to the
  ...   # tf.function
  ...   return np.sinh(x)
  >>> @tf.function(input_signature=[tf.TensorSpec(None, tf.float32)])
  ... def tf_function(input):
  ...   y = tf.numpy_function(my_numpy_func, [input], tf.float32)
  ...   return y * y
  >>> tf_function(tf.constant(1.))
  <tf.Tensor: shape=(), dtype=float32, numpy=1.3810978>

  Comparison to `tf.py_function`:
  `tf.py_function` and `tf.numpy_function` are very similar, except that
  `tf.numpy_function` takes numpy arrays, and not `tf.Tensor`s. If you want the
  function to contain `tf.Tensors`, and have any TensorFlow operations executed
  in the function be differentiable, please use `tf.py_function`.

  Note: The `tf.numpy_function` operation has the following known
  limitations:

  * The body of the function (i.e. `func`) will not be serialized in a
    `tf.SavedModel`. Therefore, you should not use this function if you need to
    serialize your model and restore it in a different environment.

  * The operation must run in the same address space as the Python program
    that calls `tf.numpy_function()`. If you are using distributed
    TensorFlow, you must run a `tf.distribute.Server` in the same process as the
    program that calls `tf.numpy_function`  you must pin the created
    operation to a device in that server (e.g. using `with tf.device():`).

  * Since the function takes numpy arrays, you cannot take gradients
    through a numpy_function. If you require something that is differentiable,
    please consider using tf.py_function.

  * The resulting function is assumed stateful and will never be optimized.

  Args:
    func: A Python function, which accepts `numpy.ndarray` objects as arguments
      and returns a list of `numpy.ndarray` objects (or a single
      `numpy.ndarray`). This function must accept as many arguments as there are
      tensors in `inp`, and these argument types will match the corresponding
      `tf.Tensor` objects in `inp`. The returns `numpy.ndarray`s must match the
      number and types defined `Tout`.
      Important Note: Input and output `numpy.ndarray`s of `func` are not
        guaranteed to be copies. In some cases their underlying memory will be
        shared with the corresponding TensorFlow tensors. In-place modification
        or storing `func` input or return values in python datastructures
        without explicit (np.)copy can have non-deterministic consequences.
    inp: A list of `tf.Tensor` objects.
    Tout: A list or tuple of tensorflow data types or a single tensorflow data
      type if there is only one, indicating what `func` returns.
    name: (Optional) A name for the operation.

  Returns:
    Single or list of `tf.Tensor` which `func` computes.
  """
  return py_func_common(func, inp, Tout, stateful=True, name=name)


ops.NotDifferentiable("PyFunc")
ops.NotDifferentiable("PyFuncStateless")
