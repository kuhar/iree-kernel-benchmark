from utils import *
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import iree.turbine.kernel as tk
import iree.turbine.kernel.lang as tkl
import iree.turbine.kernel.wave as tkw
from iree.turbine.kernel.lang.global_symbols import *
import torch

@dataclass
class GemmConfig:
    M: int
    N: int
    K: int
    tA: str
    tB: str
    operand_element_type: str
    accumulator_element_type: str
    result_element_type: str

    def get_name(self) -> str:
        name = f"gemm_{self.M}_{self.N}_{self.K}_{self.operand_element_type}_{self.accumulator_element_type}"
        if self.tA == "T":
            name += "_tA"
        elif self.tB == "T":
            name += "_tB"
        return name

    def get_inp1(self) -> str:
        if self.tA == "T":
            inp1 = f"{self.K}x{self.M}x{self.operand_element_type}"
        else:
            inp1 = f"{self.M}x{self.K}x{self.operand_element_type}"
        return inp1

    def get_inp2(self) -> str:
        if self.tB == "T":
            inp2 = f"{self.N}x{self.K}x{self.operand_element_type}"
        else:
            inp2 = f"{self.K}x{self.N}x{self.operand_element_type}"
        return inp2

    def get_byte_count(self) -> int:
        operand_element_type_bits_map = {
            "f32": 32,
            "f16": 16,
            "bf16": 16,
            "f8E4M3FNUZ": 8,
            "i8": 8,
            "i32": 32,
        }
        bytes_per_element = operand_element_type_bits_map[self.operand_element_type] // 8
        element_count = self.M * self.K + self.N * self.K + self.M * self.N
        byte_count = element_count * bytes_per_element
        return byte_count

    def get_flops(self) -> int:
        flops = 2 * self.M * self.N * self.K
        return flops

def generate_mlir(config: GemmConfig):
    K = config.K
    M = config.M
    N = config.N
    operand_element_type = config.operand_element_type
    acc_element_type = config.accumulator_element_type
    result_element_type = config.result_element_type
    tA = config.tA
    tB = config.tB
    mlir_template_A = f"""
module {{
    func.func @main(%arg0: tensor<{K}x{M}x{operand_element_type}>, %arg1: tensor<{K}x{N}x{operand_element_type}>) -> tensor<{M}x{N}x{result_element_type}> {{
        %cst = arith.constant 0.000000e+00 : {acc_element_type}
        %0 = tensor.empty() : tensor<{M}x{N}x{acc_element_type}>
        %1 = linalg.fill ins(%cst : {acc_element_type}) outs(%0 : tensor<{M}x{N}x{acc_element_type}>) -> tensor<{M}x{N}x{acc_element_type}>
        %2 = linalg.matmul_transpose_a ins(%arg0, %arg1 : tensor<{K}x{M}x{operand_element_type}>, tensor<{K}x{N}x{operand_element_type}>)
                                       outs(%1 : tensor<{M}x{N}x{acc_element_type}>)
          -> tensor<{M}x{N}x{acc_element_type}>
        %3 = arith.truncf %2 : tensor<{M}x{N}x{acc_element_type}> -> tensor<{M}x{N}x{result_element_type}>
        return %3 : tensor<{M}x{N}x{result_element_type}>
    }}
}}
"""

    mlir_template_B = f"""
module {{
    func.func @main(%arg0: tensor<{M}x{K}x{operand_element_type}>, %arg1: tensor<{N}x{K}x{operand_element_type}>) -> tensor<{M}x{N}x{result_element_type}> {{
        %cst = arith.constant 0.000000e+00 : {acc_element_type}
        %0 = tensor.empty() : tensor<{M}x{N}x{acc_element_type}>
        %1 = linalg.fill ins(%cst : {acc_element_type}) outs(%0 : tensor<{M}x{N}x{acc_element_type}>) -> tensor<{M}x{N}x{acc_element_type}>
        %2 = linalg.matmul_transpose_b ins(%arg0, %arg1 : tensor<{M}x{K}x{operand_element_type}>, tensor<{N}x{K}x{operand_element_type}>)
                                       outs(%1 : tensor<{M}x{N}x{acc_element_type}>)
          -> tensor<{M}x{N}x{acc_element_type}>
        %3 = arith.truncf %2 : tensor<{M}x{N}x{acc_element_type}> -> tensor<{M}x{N}x{result_element_type}>
        return %3 : tensor<{M}x{N}x{result_element_type}>
    }}
}}
"""

    mlir_template = f"""
module {{
    func.func @main(%arg0: tensor<{M}x{K}x{operand_element_type}>, %arg1: tensor<{K}x{N}x{operand_element_type}>) -> tensor<{M}x{N}x{result_element_type}> {{
        %cst = arith.constant 0.000000e+00 : {acc_element_type}
        %0 = tensor.empty() : tensor<{M}x{N}x{acc_element_type}>
        %1 = linalg.fill ins(%cst : {acc_element_type}) outs(%0 : tensor<{M}x{N}x{acc_element_type}>) -> tensor<{M}x{N}x{acc_element_type}>
        %2 = linalg.matmul ins(%arg0, %arg1 : tensor<{M}x{K}x{operand_element_type}>, tensor<{K}x{N}x{operand_element_type}>)
                           outs(%1 : tensor<{M}x{N}x{operand_element_type}>)
          -> tensor<{M}x{N}x{acc_element_type}>
        %3 = arith.truncf %2 : tensor<{M}x{N}x{acc_element_type}> -> tensor<{M}x{N}x{result_element_type}>
        return %3 : tensor<{M}x{N}x{result_element_type}>
    }}
}}
"""
    if tA == "T":
        return mlir_template_A
    if tB == "T":
        return mlir_template_B
    return mlir_template

def generate_tk_mlir(config: GemmConfig):
    # Input sizes
    M = tkl.sym.M
    N = tkl.sym.N
    K = tkl.sym.K
    # Workgroup tile sizes
    BLOCK_M = tkl.sym.BLOCK_M
    BLOCK_N = tkl.sym.BLOCK_N
    BLOCK_K = tkl.sym.BLOCK_K
    # Address space (for GPU, shared(1) or global(0))
    ADDRESS_SPACE = tkl.sym.ADDRESS_SPACE
    # Other hyperparameters
    LOAD_ELEMS_PER_THREAD = tkl.sym.LOAD_ELEMS_PER_THREAD
    STORE_ELEMS_PER_THREAD = tkl.sym.STORE_ELEMS_PER_THREAD

    # Expose user-constraints
    constraints: list[tkw.Constraint] = [tkw.WorkgroupConstraint(M, BLOCK_M, 0)]
    constraints += [tkw.WorkgroupConstraint(N, BLOCK_N, 1)]
    constraints += [tkw.TilingConstraint(K, BLOCK_K)]
    constraints += [tkw.WaveConstraint(M, BLOCK_M / 2)]
    constraints += [tkw.WaveConstraint(N, BLOCK_N / 2)]

    constraints += [
        tkw.HardwareConstraint(threads_per_wave=64, waves_per_block=(2, 2, 1))
    ]

    # Wave-level micro-kernel.
    # Since warps are not directly addressable, there is no
    # explicit notion of a warp id (like a workgroup or thread id).
    # This kernel uses the input sizes M, N, K throughout, as the tiling
    # and data movement strategy is determined during the compilation process.
    # These can be influenced by introducing constraints.
    @tkw.wave(constraints)
    def gemm(
        a: tkl.Memory[M, K, ADDRESS_SPACE, tkl.f16],
        b: tkl.Memory[N, K, ADDRESS_SPACE, tkl.f16],
        c: tkl.Memory[M, N, GLOBAL_ADDRESS_SPACE, tkl.f32],
    ):
        c_reg = tkl.Register[M, N, tkl.f32](0.0)

        # This microkernel encodes the fact that if the reduction
        # dimension were tiled, then we would need to materialize a loop.
        @tkw.reduction(K, init_args=[c_reg])
        def repeat(acc: tkl.Register[M, N, tkl.f32]) -> tkl.Register[M, N, tkl.f32]:
            # a_reg: tkw.Register[M, K, tkl.f16]
            a_reg = tkw.read(a, elements_per_thread=LOAD_ELEMS_PER_THREAD)
            # b_reg: tkw.Register[N, K, tkl.f16]
            b_reg = tkw.read(b, elements_per_thread=LOAD_ELEMS_PER_THREAD)
            # acc: tkw.Register[M, N, tkl.f32]
            acc = tkw.mma(a_reg, b_reg, acc)
            return acc

        # repeat represents the results of the loop
        tkw.write(repeat, c, elements_per_thread=STORE_ELEMS_PER_THREAD)

    shape = [config.M, config.N, config.K]
    operand_element_type_map = {
        "f16": torch.float16,
    }
    operand_element_type = operand_element_type_map[config.operand_element_type]

    hyperparams = {
        ADDRESS_SPACE: SHARED_ADDRESS_SPACE,
        LOAD_ELEMS_PER_THREAD: 4,
        STORE_ELEMS_PER_THREAD: 4,
        BLOCK_M: 64,
        BLOCK_N: 64,
        BLOCK_K: 32,
        M: shape[0],
        N: shape[1],
        K: shape[2],
    }
    config = {"backend": "rocm", "device": "hip", "target": "gfx942"}
    with tk.gen.TestLaunchContext(
        hyperparams, canonicalize=True, run=True, run_config=config
    ):
        a = torch.randn(shape[0], shape[2], operand_element_type=operand_element_type)
        b = torch.randn(shape[1], shape[2], operand_element_type=operand_element_type)
        c = torch.zeros(shape[0], shape[1], operand_element_type=torch.float32)
        mb = gemm(a, b, c)

        return mb.module_op.get_asm()


def compile_gemm_config(
    config: GemmConfig, kernel_dir: Path, vmfb_dir: Path, target, extra_compiler_args, tk
) -> tuple[Path, Optional[Path]]:
    mlir_file = kernel_dir / (config.get_name() + ".mlir")
    vmfb_file = vmfb_dir / (config.get_name() + ".vmfb")

    if not os.path.exists(vmfb_dir):
        os.makedirs(vmfb_dir)

    # Generate mlir content
    if tk:
        mlir_content = generate_tk_mlir(config)
    else:
        mlir_content = generate_mlir(config)

    # Write MLIR content to file
    with open(mlir_file, "w") as f:
        f.write(mlir_content)

    # Compile MLIR to VMFB
    exec_args = [
        "iree-compile",
        f"{mlir_file}",
        "--iree-hal-target-backends=rocm",
        f"--iree-hip-target={target}",
        "--iree-llvmgpu-enable-prefetch=true",
        "-o",
        f"{vmfb_file}",
    ] + extra_compiler_args

    print(" ".join(exec_args))

    ret_value, stderr = run_iree_command(exec_args)
    if ret_value == 0:
        print(f"Successfully compiled {mlir_file} to {vmfb_file}")
    else:
        error_file = vmfb_dir / (config.get_name() + "_error.txt")
        print(f"Failed to compile {mlir_file}. Error dumped in {error_file}")
        with open(error_file, "w") as f:
            f.write(stderr.decode("utf-8"))
        return mlir_file, None

    return mlir_file, vmfb_file
