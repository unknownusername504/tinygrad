# a python uops emulator
# works to test the tensor cores, and all the uops in general
# this is the (living) definition of uops
from typing import Callable, Tuple, List, Optional, Any, Dict
import pickle, base64, itertools, time, math
from tinygrad.dtype import DType, dtypes, ImageDType
from tinygrad.helpers import all_same, getenv, flatten
from tinygrad.device import Compiled, Allocator, Compiler
from tinygrad.codegen.uops import UOp, UOps
from tinygrad.ops import Op, UnaryOps, BinaryOps, TernaryOps
from tinygrad.codegen.kernel import LinearizerOptions

def check_types(arg, p, dtype):
  types = [dtypes.type(x) for x in p] + [dtype]
  if not dtypes.is_uniform_types(*types):
    raise TypeError(f"All elements in p must be of the same basic type, got {types} for {arg} {dtype} {p}")

def patch_p(p, dtype):
  # FIXME: This is a temporary fix for bad dtype inference for bools vs 0./1. ints/floats
  # Upcast bools to the highest order type
  if any(dtypes.is_bool(dtypes.type(x)) for x in p):
    patched_p = [x if not dtypes.is_bool(dtypes.type(x)) else dtypes.as_type(x, dtype) for x in p]
    p = tuple(patched_p)
  types = [dtypes.type(x) for x in p] + [dtype]
  dtype = dtypes.get_highest_order(*types)
  return p, dtype

def exec_alu(arg, dtype, p):
  if arg is TernaryOps.WHERE:
    # Attempt to force first operand to bool if it is 0/1
    p_0 = dtypes.as_type(p[0], dtypes.bool)
    patched, dtype = patch_p(p[1:], dtype)
    p = (p_0,) + patched
    check_types(arg, p[1:], dtype)
  else:
    p, dtype = patch_p(p, dtype)
    check_types(arg, p, dtype)

  operations: Dict[Op, Callable] ={
    TernaryOps.MULACC: lambda: p[0]*p[1]+p[2] if len(p) == 3 and (dtypes.is_int(dtype) or dtypes.is_float(dtype)) else None,
    TernaryOps.WHERE: lambda: p[1] if p[0] else p[2] if len(p) == 3 and dtypes.is_bool(dtypes.type(p[0])) else None,
    UnaryOps.LOG2: lambda: math.log2(p[0]) if len(p) == 1 and p[0] > 0 and dtypes.is_float(dtype) else math.nan,
    UnaryOps.EXP2: lambda: math.exp2(p[0]) if len(p) == 1 and dtypes.is_float(dtype) else None,
    UnaryOps.SQRT: lambda: math.sqrt(p[0]) if len(p) == 1 and p[0] >= 0 and dtypes.is_float(dtype) else math.nan,
    UnaryOps.SIN: lambda: math.sin(p[0]) if len(p) == 1 and dtypes.is_float(dtype) else None,
    UnaryOps.NEG: lambda: -p[0] if len(p) == 1 and (dtypes.is_int(dtype) or dtypes.is_float(dtype)) else None,
    BinaryOps.MUL: lambda: p[0]*p[1] if len(p) == 2 and (dtypes.is_int(dtype) or dtypes.is_float(dtype)) else None,
    BinaryOps.ADD: lambda: p[0]+p[1] if len(p) == 2 and (dtypes.is_int(dtype) or dtypes.is_float(dtype)) else None,
    BinaryOps.SUB: lambda: p[0]-p[1] if len(p) == 2 and (dtypes.is_int(dtype) or dtypes.is_float(dtype)) else None,
    BinaryOps.XOR: lambda: p[0]^p[1] if len(p) == 2 and dtypes.is_int(dtype) and not dtypes.is_unsigned(dtype) else None,
    BinaryOps.MAX: lambda: max(p[0], p[1]) if len(p) == 2 and (dtypes.is_int(dtype) or dtypes.is_float(dtype)) else None,
    BinaryOps.CMPEQ: lambda: p[0] == p[1] if len(p) == 2 and (dtypes.is_int(dtype) or dtypes.is_float(dtype)) else None,
    BinaryOps.CMPLT: lambda: p[0] < p[1] if len(p) == 2 and (dtypes.is_int(dtype) or dtypes.is_float(dtype)) else None,
    BinaryOps.DIV: lambda: p[0]//p[1] if len(p) == 2 and dtypes.is_int(dtype) and p[1] != 0 else (p[0]/p[1] if p[1] != 0 else math.nan) if dtypes.is_float(dtype) else None,
    BinaryOps.MOD: lambda: p[0]%p[1] if len(p) == 2 and dtypes.is_int(dtype) and not dtypes.is_unsigned(dtype) and p[1] != 0 else None
  }

  if arg not in operations:
    raise NotImplementedError(f"Unsupported operation: {arg}")
  result = operations[arg]()
  if result is None:
    raise ValueError(f"Invalid operands for operation {arg}: {p}")
  
  try:
    # FIXME: This is a temporary fix for bad result dtype inference for floats that are too large
    try:
      dtypes.check_bounds(result, dtype)
    except OverflowError:
      # Try to increase the precision of the result
      if dtypes.is_float(dtype):
        dtype = dtypes.double
      elif dtypes.is_unsigned(dtype):
        dtype = dtypes.ulong
      elif dtypes.is_int(dtype):
        dtype = dtypes.long
    result = dtypes.as_type(result, dtype)
  except TypeError as e:
    error_msg = f"Result {result} cannot be converted to {dtype} for operation {arg} and operands {p}"
    raise TypeError(error_msg) from e
  except OverflowError as e:
    error_msg = f"Result {result} overflows {dtype} (upcast failed) for operation {arg} and operands {p}"
    raise OverflowError(error_msg) from e

  return result

def _load(m, i):
  if i<0 or i>=len(m): raise IndexError(f"load out of bounds, size is {len(m)} and access is {i}")
  return m[i]
def load(inp, j=0):
  if len(inp) == 4:
    return [_load(m, x+j) if gate else default for m,x,gate,default in zip(*inp)]
  else:
    return [_load(m, x+j) for m,x in zip(inp[0], inp[1])]

def _store(m, i, v):
  if i<0 or i>=len(m): raise IndexError(f"store out of bounds, size is {len(m)}, access is {i}, value is {v}")
  m[i] = v

class PythonProgram:
  def __init__(self, name:str, lib:bytes):
    self.uops: List[Tuple[UOps, Optional[DType], List[int], Any]] = pickle.loads(lib)
  def __call__(self, *bufs, global_size:Tuple[int,int,int]=(1,1,1), local_size:Tuple[int,int,int]=(1,1,1), vals:Tuple[int, ...]=(), wait=False):
    st = time.perf_counter()
    warp = list(itertools.product(*[range(x) for x in local_size[::-1]]))
    warp_size = len(warp)
    for idxs in itertools.product(*[range(x) for x in global_size[::-1]]):
      ul: Dict[int, Any] = {}
      dl: Dict[int, DType] = {}
      pbufs: List[memoryview] = list(bufs)
      i = 0
      loop_ends: Dict[int, int] = {}
      while i < len(self.uops):
        uop, dtype, idp, arg = self.uops[i]
        void_ops = {UOps.STORE, UOps.ENDLOOP, UOps.BARRIER, UOps.IF, UOps.ENDIF}
        inp = [ul[v] for v in idp if self.uops[v][0] not in void_ops]
        dtp = [dl[v] for v in idp if self.uops[v][0] not in void_ops]
        if getenv("TRACE"): print(i, uop, dtype, arg, inp, dtp)
        if uop is UOps.STORE:
          assert len(inp) <= 3, "gated stores not supported yet"
          if isinstance(dtp[0], ImageDType):
            # image store
            assert dtp[2].count == 4
            for j,val in enumerate(inp[2]):
              for m,ox,oy,v in zip(inp[0], inp[1][0], inp[1][1], val):
                assert ox >= 0 and ox < dtp[0].shape[1] and oy >= 0 and oy < dtp[0].shape[0]
                _store(m, ox*4 + oy*dtp[0].shape[1]*4 + j, v)
          elif dtp[2].count > 1:
            for j,val in enumerate(inp[2]):
              for m,o,v in zip(inp[0], inp[1], val): _store(m, o+j, v)
          else:
            for m,o,v in zip(*inp): _store(m, o, v)
          i += 1
          continue
        elif uop is UOps.ENDLOOP:
          loop_ends[idp[0]] = i
          i = idp[0]
          continue
        elif uop in (UOps.BARRIER, UOps.IF, UOps.ENDIF):
          # in the python emulator, the warp is always in sync
          i += 1
          continue
        assert dtype is not None, f"{uop} is missing a dtype"
        dl[i] = dtype
        if uop is UOps.DEFINE_GLOBAL:
          assert dtype.fmt is not None
          ul[i] = [pbufs.pop(0).cast(dtype.fmt)] * warp_size
        elif uop is UOps.DEFINE_LOCAL:
          assert dtype.fmt is not None
          lbuf = memoryview(bytearray(arg[1]*dtype.itemsize))
          ul[i] = [lbuf.cast(dtype.fmt)] * warp_size
        elif uop is UOps.SPECIAL:
          if arg[1][0] == 'g':
            ul[i] = [idxs[2-arg[0]]] * warp_size
          elif arg[1][0] == 'l':
            ul[i] = [x[2-arg[0]] for x in warp]
        elif uop is UOps.CONST:
          if dtype.count > 1:
            ul[i] = [[dtypes.as_type(arg, dtype)] * warp_size for _ in range(dtype.count)]
          else:
            ul[i] = [dtypes.as_type(arg, dtype)] * warp_size
        elif uop is UOps.DEFINE_ACC:
          if dtype.count > 1:
            ul[i] = [[arg] * warp_size for _ in range(dtype.count)]
          else:
            ul[i] = [arg] * warp_size
        elif uop is UOps.LOOP:
          if i not in ul:
            ul[i] = [inp[0][0]] * warp_size
          else:
            for j in range(len(ul[i])):
              ul[i][j] += 1
            if ul[i][0] == inp[1][0]:
              del ul[i]
              i = loop_ends[i] + 1
              continue
        elif uop is UOps.CAST:
          if dtype.count > 1:
            ul[i] = inp
          else:
            try:
              ul[i] = [dtypes.as_type(x, dtype) for x in inp[0]]
            except Exception:
              ul[i] = inp[0]
        elif uop is UOps.LOAD:
          if isinstance(dtp[0], ImageDType):
            assert dtype.count == 4
            ul[i] = []
            for j in range(dtype.count):
              ret = []
              for m,ox,oy in zip(inp[0], inp[1][0], inp[1][1]):
                if ox < 0 or ox >= dtp[0].shape[1] or oy < 0 or oy >= dtp[0].shape[0]: ret.append(0)
                else: ret.append(_load(m, ox*4 + oy*dtp[0].shape[1]*4 + j))
              ul[i].append(ret)
          elif dtype.count > 1:
            ul[i] = [load([inp[i][j] if dtp[i].count > 1 else inp[i] for i in range(len(inp))], j) for j in range(dtype.count)]
          else:
            ul[i] = load(inp)
        elif uop is UOps.PHI:
          for j in range(len(inp[0])):
            inp[0][j] = inp[1][j]
          ul[i] = inp[0]
        elif uop is UOps.GEP:
          ul[i] = inp[0][arg]
        elif uop is UOps.WMMA:
          # here are the models for the WMMA instruction on the different hardware
          def wmma_helper(WARP_THREADS, K, NUM_A, NUM_B, NUM_C, a_elem, b_elem, c_map):
            assert len(inp[0]) == NUM_A, f"A must have {NUM_A} elements per thread"
            assert len(inp[1]) == NUM_B, f"B must have {NUM_B} elements per thread"
            assert len(inp[2]) == NUM_C, f"C must have {NUM_C} elements per thread"
            assert len(flatten(inp[0])) == NUM_A * warp_size, f"WMMA must have {NUM_A * warp_size} total elements for A in WMMA"
            assert len(flatten(inp[1])) == NUM_B * warp_size, f"WMMA must have {NUM_B * warp_size} total elements for B in WMMA"
            assert len(flatten(inp[2])) == NUM_C * warp_size, f"WMMA must have {NUM_C * warp_size} total elements for C in WMMA"
            assert warp_size > 0 and warp_size % WARP_THREADS == 0, f"must have multiples of {WARP_THREADS} warp threads"
            out = [inp[2][elem_idx][:] for elem_idx in range(NUM_C)]
            for goff in range(0, warp_size, WARP_THREADS):
              for lane_id in range(WARP_THREADS):
                for elem_idx in range(NUM_C): # calculate new muls and add to acc
                  (c_i, c_j) = c_map(lane_id, elem_idx)
                  out[elem_idx][goff+lane_id] += sum(a_elem(inp[0], _k, c_j, goff) * b_elem(inp[1], c_i, _k, goff) for _k in range(K))
            return out

          if arg.startswith('__metal_wmma'):
            def a_b_elem(x, i, j, goff): # A (2 elements on 32 threads): row major
              return x[(i%2)][goff+(i//2)%2+(j%4)*2+(i//4)*8+(j//4)*16]
            def c_map(lane, elem): # (i, j), C, D (2 elements on 32 threads): row major same as A/B
              return (elem + ((lane%2)*2) + ((lane//8)%2)*4, ((lane//2)%4) + (lane//16)*4)
            ul[i] = wmma_helper(32, 8, 2, 2, 2, a_b_elem, a_b_elem, c_map)
          elif arg == '__builtin_amdgcn_wmma_f32_16x16x16_f16_w32' or arg == '__hip_wmma_f16_f16':
            def a_elem(x, i, j, goff): # A (16 elements on 32 threads): col major, lane 16-32 == lane 0-15
              assert x[i][goff+j] == x[i][goff+j+16], "warp elements not duplicated properly across lanes"
              return x[i][goff+j]
            def b_elem(x, i, j, goff): # B (16 elements on 32 threads): row major, lane 16-32 == lane 0-15
              return a_elem(x, j, i, goff)
            def c_map(lane, elem): return (lane%16, lane//16+elem*2) # (i, j), C, D (8 elements on 32 threads): row major
            ul[i] = wmma_helper(32, 16, 16, 16, 8, a_elem, b_elem, c_map)
          else:
            raise Exception(f"unimplemented tensor core {arg}")
        elif uop is UOps.ALU:
          assert all_same([len(x) for x in inp]), f"{[len(x) for x in inp]} doesn't match on {arg}"
          assert all_same([dtype] + dtp) or arg in {BinaryOps.CMPEQ, BinaryOps.CMPLT, TernaryOps.WHERE}, f"dtype mismatch on {arg}"
          ul[i] = [exec_alu(arg, dtype, p) for p in zip(*inp)]
        assert i in ul, (uop, dtype, idp, arg)
        i += 1
    return time.perf_counter() - st

class PythonCompiler(Compiler):
  linearizer_opts = LinearizerOptions("METAL", has_tensor_cores=True) if getenv("EMULATE_METAL") else \
    (LinearizerOptions("HIP", has_tensor_cores=True) if getenv("EMULATE_HIP") else LinearizerOptions())
  def render(self, name:str, uops:List[UOp]) -> str:
    lops = [(u.uop, u.dtype, [uops.index(v) for v in u.vin], u.arg) for u in uops]
    return base64.b64encode(pickle.dumps(lops)).decode()
  def compile(self, src:str) -> bytes: return base64.b64decode(src)

class PythonAllocator(Allocator):
  def _alloc(self, size): return memoryview(bytearray(size))
  def copyin(self, dest, src:memoryview): dest[:] = src
  def copyout(self, dest:memoryview, src): dest[:] = src

class PythonDevice(Compiled):
  def __init__(self, device:str):
    super().__init__(device, PythonAllocator(), PythonCompiler(), PythonProgram)
