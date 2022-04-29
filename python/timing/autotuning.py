import torch
from torch import nn
from rz_linear import RzLinear
from rz_linear.impl.RzLinearForward import rz_linear_forward_tl
from rz_linear.impl.RzLinearBackward import rz_linear_backward_tl
from decorators import timing
from rz_linear.RzLinearFunction import RzLinearFunction
import triton

device = torch.device('cuda:0')

ITERS = 5

SHARED = 48 * 1024

# most conservation search
# vectorized: 4
# two buffers: 2
STAGE_SIZE = 8

triton_configs = []


def shared_memory_constraint(m, n, k, num_warps, num_stages):
    return (m * n + STAGE_SIZE * num_warps * 32) * 4 < SHARED


def generate_configs():
    for m in [16, 32, 64, 128, 256]:
        for n in [16, 32, 64, 128, 256]:
            for k in [16, 32, 64, 128, 256]:
                for num_stages in [2, 3, 4]:
                    for num_warps in [4, 8]:
                        if shared_memory_constraint(m, n, k, num_warps, num_stages) and \
                                shared_memory_constraint(k, n, m, num_warps, num_stages) and shared_memory_constraint(m, k, n, num_warps, num_stages):
                            triton_configs.append(triton.Config(
                                {'BLOCK_SIZE_M': m, 'BLOCK_SIZE_K': k, 'BLOCK_SIZE_N': n}, num_stages=num_stages, num_warps=num_warps))


def autotune(batch_sizes, shapes, mem_sizes, allow_tf32=False):
    # layer-wise autotuning
    # tf32: 10 minutes per shape
    # fp32: 40 minutes per shape
    # The autotuning process should be improved, but it is not the goal of this project.
    generate_configs()
    for batch_size in batch_sizes:
        for shape in shapes:
            input_dim, output_dim = shape[0], shape[1]
            x = torch.rand((batch_size, input_dim), device=device)
            for mem_size in mem_sizes:
                weight = torch.arange(
                    mem_size, device=device).type(torch.float32)
                rz = RzLinear(input_dim, output_dim,
                              hashed_weight=weight, seed=1367, device=device)
                R7, R6, R5, R4 = rz._random_numbers[7].item(), rz._random_numbers[6].item(
                ), rz._random_numbers[5].item(), rz._random_numbers[4].item()
                R3, R2, R1, R0 = rz._random_numbers[3].item(), rz._random_numbers[2].item(
                ), rz._random_numbers[1].item(), rz._random_numbers[0].item()
                M, K, N, H = batch_size, input_dim, output_dim, mem_size
                # warmup
                rz_linear_forward_tl(x, weight, M, K, N, H, R7, R6, R5, R4,
                                     R3, R2, R1, R0, allow_tf32=allow_tf32, allow_autotune=False)

                def bench():
                    for _ in range(ITERS):
                        output = rz_linear_forward_tl(x, weight, M, K, N, H, R7, R6, R5, R4, R3, R2, R1, R0,
                                                      allow_tf32=allow_tf32, allow_autotune=False,
                                                      BLOCK_SIZE_K=BLOCK_SIZE_K, BLOCK_SIZE_N=BLOCK_SIZE_N, BLOCK_SIZE_M=BLOCK_SIZE_M,
                                                      num_stages=num_stages, num_warps=num_warps)
                        input_grad, weight_grad = rz_linear_backward_tl(x, weight, output, M, K, N, H, R7, R6, R5, R4, R3, R2, R1, R0,
                                                                        allow_tf32=allow_tf32, allow_autotune=False,
                                                                        BLOCK_SIZE_K=BLOCK_SIZE_K, BLOCK_SIZE_N=BLOCK_SIZE_N, BLOCK_SIZE_M=BLOCK_SIZE_M,
                                                                        num_stages=num_stages, num_warps=num_warps)
                fast_time = 0.0
                for config in triton_configs:
                    BLOCK_SIZE_M = config.kwargs['BLOCK_SIZE_M']
                    BLOCK_SIZE_N = config.kwargs['BLOCK_SIZE_N']
                    BLOCK_SIZE_K = config.kwargs['BLOCK_SIZE_K']
                    num_warps = config.num_warps
                    num_stages = config.num_stages
                    try:
                        t, _ = timing(bench)()
                        #print('{}: {}'.format(config, t))
                        if fast_time == 0.0 or t < fast_time:
                            fast_time = t
                            fast_config = (BLOCK_SIZE_M, BLOCK_SIZE_N,
                                           BLOCK_SIZE_K, num_warps, num_stages)
                    except:
                        # TODO(Keren): exception dictionary using unsorted BLOCK_SIZEs
                        print('{}: except'.format(config))
                if fast_time != 0.0:
                    print('{} {}: {}'.format((M, K, N, H), config, fast_time))
                    RzLinearFunction._memoize_dict[(M, K, N, H)] = fast_config