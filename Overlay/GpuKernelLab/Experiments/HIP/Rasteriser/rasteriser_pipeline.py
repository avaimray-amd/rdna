import pathlib
import dataclasses
from unittest.mock import patch

from pygpudirect import AcquireCacheFlags, IndirectDispatchDesc, ReleaseCacheFlags
from pygpudirect.helper.pm4_builder import IT_DISPATCH_INDIRECT, PM4Builder, pm4_type3_hdr

from Experiments.HIP.hip_experiment import HIPExperiment
from Experiments.utils import div_ceil

import torch
import torchvision
import numpy as np


class HIP_RasteriserPipeline(HIPExperiment):
    KERNEL_NAME = "rasteriser_pipeline"
    BIN_KERNEL_NAME = "raster_bin_pipeline"
    REUSE_KERNEL_NAME = "reuse_pipeline"
    VERTEX_KERNEL_NAME = "vertex_pipeline"
    FRAGMENT_KERNEL_NAME = "fragment_pipeline"
    BLOCK_THREADS_X = 32
    REUSE_TRIANGLES_PER_WAVE = 10
    RASTER_THREADS_X = 512
    BIN_THREADS_X = 32  # bin pass default; overridden by Problem.bin_threads
    SETUP_STRIDE = 16  # pre-routed: 6 (m, k) pairs already split into lo/hi, then the bbox

    @dataclasses.dataclass
    class Problem:
        width: int = 1920
        height: int = 1080
        num_triangles: int = 4096
        tri_size: float = 0.05  # vertex jitter box, as a fraction of NDC
        tri_size_spread: float = 1.0  # >1 draws each size log-uniformly in [size/s, size*s]
        queue_capacity: int = 4096  # triangle references per queue before the fallback
        records_in_flight: int = 1
        skip_stores: int = 0  # measurement only: drops the pixel writes
        workgroups: int = 1
        #One wave per workgroup, matching the raster pass. The bin pass has no
        #cross-lane or cross-wave interaction, so nothing larger buys anything, and
        #the kernel's amdgpu_flat_work_group_size(32, 32) requires it.
        bin_threads: int = 32
        output: str = "Images/rasteriser_pipeline.png"

    @property
    def num_pixels(self):
        return self.problem.width * self.problem.height

    @property
    def num_indices(self):
        return self.problem.num_triangles * 3

    @property
    def tiles_x(self):
        return div_ceil(self.problem.width, 32)

    @property
    def tiles_y(self):
        return div_ceil(self.problem.height, 32)

    @property
    def num_tiles(self):
        return self.tiles_x * self.tiles_y

    #multi_processor_count counts WGPs; an RDNA WGP holds 4 SIMD32.
    @property
    def num_simds(self):
        #PyGpuDirect reports no CU count, so --workgroups has to supply it there
        props = getattr(self.context, "device_properties", None)
        if props is None:
            return self.problem.workgroups
        return props.multi_processor_count * 4

    @property
    def num_workgroups(self):
        return self.problem.workgroups or self.num_simds // 2

    @property
    def num_queues(self):
        return self.num_workgroups * 2

    @property
    def grid_blocks(self):
        return [self.num_workgroups, 1, 1]

    @property
    def block_threads(self):
        return [self.RASTER_THREADS_X, 1, 1]

    @property
    def bin_threads_x(self):
        return self.problem.bin_threads or self.BIN_THREADS_X

    @property
    def bin_grid_blocks(self):
        return [div_ceil(self.problem.num_triangles, self.bin_threads_x), 1, 1]

    @property
    def bin_block_threads(self):
        return [self.bin_threads_x, 1, 1]

    @property
    def bin_kernel_params(self):
        return [self.queue_count, self.queue_list, self.overflow, self.setup,
            self.shaded_positions, self.connectivity]

    @property
    def kernel_params(self):
        return [self.coverage, self.queue_count, self.queue_list, self.setup]

    def create_args(self, parser):
        super().create_args(parser)
        parser.add_arguments(self.Problem, dest="problem")

    def init(self):
        if not self.hip_runtime.gpud_context or self.context.arch_name != "gfx1310":
            raise ValueError("This pipeline requires gfx1310 and PyGpuDirect indirect dispatch")
        assert self.problem.width > 1, "width must be greater than one"
        assert self.problem.height > 1, "height must be greater than one"
        assert self.problem.num_triangles > 0, "num_triangles must be positive"
        assert self.problem.queue_capacity > 0, "queue_capacity must be positive"
        assert self.num_workgroups > 0, "this backend reports no CU count: set --workgroups"

    def generate_data(self):
        #Uniformly scattered triangles: a random centre per triangle with its three
        #vertices jittered inside a tri_size box. Triangles may overlap.
        n = self.problem.num_triangles
        rng = np.random.default_rng(self.runtime.seed)
        half = self.problem.tri_size * 0.5
        #A single size makes every triangle cover about the same number of tiles, which
        #hides any work-distribution effect. Spreading them log-uniformly does not.
        if self.problem.tri_size_spread > 1.0:
            lo = np.log(half / self.problem.tri_size_spread)
            hi = np.log(min(half * self.problem.tri_size_spread, 0.5))
            half = np.exp(rng.uniform(lo, hi, size=(n, 1, 1)))
        centres = rng.uniform(half, 1.0 - half, size=(n, 1, 2))
        tris = (centres + rng.uniform(-1.0, 1.0, size=(n, 3, 2)) * half).astype(np.float32)

        #Force clockwise winding (the kernel keeps pixels where every edge gives
        #result >= 0, which is CW in the y-up NDC space it evaluates).
        ax, ay = tris[:, 0, 0], tris[:, 0, 1]
        bx, by = tris[:, 1, 0], tris[:, 1, 1]
        cx, cy = tris[:, 2, 0], tris[:, 2, 1]
        ccw = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) > 0
        tris[ccw] = tris[ccw][:, [0, 2, 1]]

        self.vertices_data = np.zeros((n * 3, 4), dtype=np.float32)
        self.vertices_data[:, :2] = tris.reshape(-1, 2)
        self.vertices_data[:, 3] = 1.0
        self.indices_data = np.arange(self.num_indices, dtype=np.uint32)

    def create_tensors(self):
        self.image = self.context.create_buffer(self.num_pixels * 4)
        self.coverage = self.context.create_buffer(self.num_pixels * 4)
        self.vertices = self.context.create_buffer(self.vertices_data.nbytes)
        self.indices = self.context.create_buffer(self.indices_data.nbytes)
        self.unique_shades = self.context.create_buffer(self.num_indices * 4)
        self.connectivity = self.context.create_buffer(self.num_indices * 4)
        self.shade_count = self.context.create_buffer(4)
        self.vertex_dispatch_args = self.context.create_buffer(12)
        self.reuse_completion = self.context.create_buffer(8)
        self.shaded_positions = self.context.create_buffer(self.num_indices * 2 * 4)
        self.setup = self.context.create_buffer(
            self.problem.num_triangles * self.SETUP_STRIDE * 4
        )
        self.queue_count = self.context.create_buffer(self.num_queues * 4)
        self.queue_list = self.context.create_buffer(
            self.num_queues * self.problem.queue_capacity * 4
        )
        self.overflow = self.context.create_buffer(4)

    def upload_data(self):
        #Both contexts accept torch tensors; only the GPUD one accepts numpy arrays
        self.context.upload_buffer(self.vertices, torch.from_numpy(self.vertices_data))
        self.context.upload_buffer(self.indices, torch.from_numpy(self.indices_data))
        self.context.upload_buffer(self.unique_shades, torch.full((self.num_indices,), -1, dtype=torch.int32))
        self.context.upload_buffer(self.connectivity, torch.full((self.num_indices,), -1, dtype=torch.int32))
        self.context.upload_buffer(self.shaded_positions, torch.full((self.num_indices * 2,), float("nan")))
        self.context.upload_buffer(self.coverage, torch.zeros(self.num_pixels, dtype=torch.int32))
        self.context.upload_buffer(self.image, torch.full((self.num_pixels,), -1, dtype=torch.int32))
        #Only cleared here: the raster pass zeroes each queue's counter on the way out.
        self.context.upload_buffer(
            self.queue_count, torch.zeros(self.num_queues, dtype=torch.int32)
        )
        self.context.upload_buffer(self.overflow, torch.zeros(1, dtype=torch.int32))

    def compile_kernels(self):
        compiler_defines = dict(
            num_pixels=self.num_pixels,
            width=self.problem.width,
            height=self.problem.height,
            num_triangles=self.problem.num_triangles,
            num_queues=self.num_queues,
            queue_capacity=self.problem.queue_capacity,
            block_threads_x=self.BLOCK_THREADS_X,
            bin_threads_x=self.bin_threads_x,
            records_in_flight=self.problem.records_in_flight,
            skip_stores=self.problem.skip_stores,
        )
        compiler_flags = [
            "-O3", "-ffast-math", "-nohipwrapperinc", "-nobuiltininc", "-nogpuinc",
            "-nostdinc++", "-mcumode", "-std=c++20",
        ]
        kernel_file = pathlib.Path(__file__).parent / "Kernels" / f"{self.KERNEL_NAME}.cpp"
        self.compiled_code = self.context.compile_kernel(
            kernel_file, compiler_defines, compiler_flags
        )

    def create_kernels(self):
        self.kernel = self.context.create_kernel(self.compiled_code)
        self.vertex_kernel = self.kernel.create_kernel(self.VERTEX_KERNEL_NAME)
        if (tuple(self.vertex_kernel.note_info.workgroup_size) != (32, 1, 1)
                or not self.vertex_kernel.metadata.enable_wavefront_size32):
            raise RuntimeError("Indirect vertex dispatch requires wave32 and 32-thread workgroup metadata")
        if (self.vertex_kernel.metadata.private_segment_fixed_size
                or self.vertex_kernel.metadata.kernel_code_properties.enable_sgpr_dispatch_ptr):
            raise RuntimeError("Indirect vertex dispatch requires a scratch-free kernel without a dispatch pointer")
        for argument, buffer in enumerate((self.vertices, self.unique_shades,
                                           self.shade_count, self.shaded_positions)):
            self.vertex_kernel.set_arg_pointer(argument, buffer)

    def dispatch_reuse(self, stream):
        stream.barrier(cache_flags=AcquireCacheFlags.WB_ALL)
        stream.cmd.fill_buffer(self.shade_count, 0, 4, 0)
        stream.cmd.fill_buffer(self.vertex_dispatch_args, 0, 4, 0)
        stream.cmd.fill_buffer(self.vertex_dispatch_args, 4, 8, 1)
        stream.cmd.fill_buffer(self.reuse_completion, 0, 8, 0)
        PM4Builder(stream.cmd._pm4, stream.cmd._SHADER_TYPE).wait_dma_data()
        stream.barrier(cache_flags=AcquireCacheFlags.WB_ALL)
        self.context.launch_kernel(
            self.kernel, self.REUSE_KERNEL_NAME,
            [div_ceil(self.problem.num_triangles, self.REUSE_TRIANGLES_PER_WAVE), 1, 1],
            [32, 1, 1],
            [self.indices, self.unique_shades, self.connectivity,
             self.shade_count, self.vertex_dispatch_args], stream,
        )
        pm4 = PM4Builder(stream.cmd._pm4, stream.cmd._SHADER_TYPE)
        pm4.release_mem(self.reuse_completion.gpu_va, 1, gcr_cntl=ReleaseCacheFlags.WB_ALL)
        pm4.wait_reg_mem(self.reuse_completion.gpu_va, 1, function=3)
        stream.barrier(cache_flags=AcquireCacheFlags.WB_ALL)

    def dispatch_vertex(self, stream):
        def record_indirect(pm4, args_gpu_va, initiator):
            if pm4.shader_type != 1:
                raise ValueError("This indirect vertex launch requires a compute command buffer")
            pm4.buf.extend((
                pm4_type3_hdr(IT_DISPATCH_INDIRECT, 4, pm4.shader_type),
                args_gpu_va & 0xFFFFFFFF,
                (args_gpu_va >> 32) & 0xFFFFFFFF,
                initiator,
            ))

        single_cu = getattr(stream, "raster_enabled_simds", None) == 2
        with patch.object(PM4Builder, "dispatch_indirect", record_indirect):
            stream.cmd.dispatch_indirect(self.vertex_kernel, IndirectDispatchDesc(
                args_buffer=self.vertex_dispatch_args,
                static_thread_mgmt_se0=1 if single_cu else 0xFFFFFFFF,
                static_thread_mgmt_se1=0 if single_cu else 0xFFFFFFFF,
                static_thread_mgmt_se2=0 if single_cu else 0xFFFFFFFF,
                static_thread_mgmt_se3=0 if single_cu else 0xFFFFFFFF,
            ))
        stream.barrier()

    def dispatch_fragment(self, stream):
        self.context.launch_kernel(
            self.kernel, self.FRAGMENT_KERNEL_NAME,
            [div_ceil(self.num_pixels, 32), 1, 1], [32, 1, 1],
            [self.coverage, self.image], stream,
        )

    def dispatch_bin(self, stream):
        self.context.launch_kernel(
            self.kernel,
            self.BIN_KERNEL_NAME,
            self.bin_grid_blocks,
            self.bin_block_threads,
            self.bin_kernel_params,
            stream,
        )

    def dispatch_raster(self, stream):
        self.context.launch_kernel(
            self.kernel,
            self.KERNEL_NAME,
            self.grid_blocks,
            self.block_threads,
            self.kernel_params,
            stream,
        )

    def dispatch(self, stream):
        timed = self.runtime.dispatch_time

        if timed:
            self.context.record_time("0_reuse", stream)
        self.dispatch_reuse(stream)
        if timed:
            self.context.record_time("0_reuse", stream)

        if timed:
            self.context.record_time("1_vertex", stream)
        self.dispatch_vertex(stream)
        if timed:
            self.context.record_time("1_vertex", stream)

        if timed:
            self.context.record_time("2_bin", stream)
        self.dispatch_bin(stream)
        if timed:
            self.context.record_time("2_bin", stream)

        if timed:
            self.context.record_time("3_raster", stream)
        self.dispatch_raster(stream)
        if timed:
            self.context.record_time("3_raster", stream)

        if timed:
            self.context.record_time("4_fragment", stream)
        self.dispatch_fragment(stream)
        if timed:
            self.context.record_time("4_fragment", stream)

    def reference_coverage(self):
        pixel_y, pixel_x = np.mgrid[:self.problem.height, :self.problem.width]
        sample_x = pixel_x / (self.problem.width - 1)
        sample_y = 1.0 - pixel_y / (self.problem.height - 1)
        covered = np.zeros(sample_x.shape, dtype=bool)
        triangles = self.vertices_data[self.indices_data, :2].reshape(-1, 3, 2)
        for triangle in triangles.astype(np.float64):
            inside = np.ones(sample_x.shape, dtype=bool)
            for edge in range(3):
                start = triangle[edge]
                end = triangle[(edge + 1) % 3]
                value = ((end[1] - start[1]) * (sample_x - start[0])
                         - (end[0] - start[0]) * (sample_y - start[1]))
                inside &= value >= 0.0
            covered |= inside
        return covered.reshape(-1)

    def validate_pipeline(self):
        shade_count = int(self.context.readback_buffer(self.shade_count).numpy().view(np.uint32)[0])
        if not 0 < shade_count <= self.num_indices:
            raise RuntimeError(f"Invalid unique shade count: {shade_count}")
        unique_shades = self.context.readback_buffer(self.unique_shades, shade_count * 4).numpy().view(np.uint32)
        connectivity = self.context.readback_buffer(self.connectivity).numpy().view(np.uint32)
        if (unique_shades >= len(self.vertices_data)).any() or (connectivity >= shade_count).any():
            raise RuntimeError("Reuse output contains an out-of-range vertex or shade ID")
        if not np.array_equal(unique_shades[connectivity], self.indices_data):
            raise RuntimeError("Connectivity does not reconstruct the input index buffer")
        expected_count = 0
        for start in range(0, self.num_indices, self.REUSE_TRIANGLES_PER_WAVE * 3):
            block_indices = self.indices_data[start:start + self.REUSE_TRIANGLES_PER_WAVE * 3]
            local_ids = {int(vertex): offset for offset, vertex in enumerate(dict.fromkeys(block_indices))}
            expected_local = np.array([local_ids[int(vertex)] for vertex in block_indices], dtype=np.uint32)
            actual = connectivity[start:start + len(block_indices)]
            if not np.array_equal(actual, expected_local + actual[0]):
                raise RuntimeError(f"Wave-local reuse is incorrect at input index {start}")
            expected_count += len(local_ids)
        if (shade_count != expected_count
                or not np.array_equal(np.unique(connectivity), np.arange(shade_count, dtype=np.uint32))):
            raise RuntimeError("Reuse output is not a densely packed list of wave-local unique shades")
        dispatch_args = self.context.readback_buffer(self.vertex_dispatch_args).numpy().view(np.uint32)
        if not np.array_equal(dispatch_args, [div_ceil(shade_count, 32), 1, 1]):
            raise RuntimeError(f"Invalid indirect vertex dispatch arguments: {dispatch_args.tolist()}")
        positions = self.context.readback_buffer(self.shaded_positions, shade_count * 2 * 4).numpy().view(np.float32).reshape(-1, 2)
        if not np.array_equal(positions, self.vertices_data[unique_shades, :2]):
            raise RuntimeError("Packed vertex output does not match input XY positions")
        coverage = self.context.readback_buffer(self.coverage).numpy().view(np.uint32)
        expected = self.reference_coverage()
        if not np.array_equal(coverage, expected.astype(np.uint32)):
            raise RuntimeError(f"Raster coverage differs at {np.count_nonzero(coverage != expected)} pixels")
        image = self.readback_data()
        expected_image = np.where(expected, np.uint32(0xFF00FF00), np.uint32(0))
        if not np.array_equal(image, expected_image):
            raise RuntimeError("Fragment output does not match reference coverage and colour")
        overflow = int(self.context.readback_buffer(self.overflow).numpy().view(np.uint32)[0])
        counters = self.context.readback_buffer(self.queue_count).numpy().view(np.uint32)
        if overflow or counters.any():
            raise RuntimeError(f"Raster queues failed: overflow={overflow}, counts={counters.tolist()}")
        self.validation_result = {
            "reuse_matches": True,
            "connectivity_matches": True,
            "unique_shades": shade_count,
            "input_indices": self.num_indices,
            "vertex_dispatch_args": dispatch_args.tolist(),
            "vertex_dispatch_indirect": True,
            "vertex_matches": True,
            "coverage_matches_cpu": True,
            "fragment_matches": True,
            "covered_pixels": int(expected.sum()),
            "uncovered_pixels": int((~expected).sum()),
            "overflow": overflow,
            "queues_drained": True,
        }
        print(f"Pipeline validation passed: {int(expected.sum())} covered pixels")

    def readback_data(self):
        raw = self.context.readback_buffer(self.image, self.num_pixels * 4)
        return raw.numpy().view(np.uint32)

    def print_bin_stats(self):
        overflow = self.context.readback_buffer(self.overflow, 4).numpy().view(np.uint32)
        print(
            f"{self.num_workgroups} raster workgroups of {self.RASTER_THREADS_X} threads "
            f"(two consumers each), {self.bin_grid_blocks[0]} bin workgroups of "
            f"{self.bin_threads_x} threads, {self.problem.num_triangles:,} triangles, "
            f"{self.num_tiles} super-tiles, {self.num_queues} queues"
        )
        if int(overflow[0]):
            print("  WARNING: a queue overflowed queue_capacity; the image is incomplete")

    def save_image(self):
        rgba = self.readback_data().view(np.uint8).reshape(
            self.problem.height, self.problem.width, 4
        )
        chw = torch.from_numpy(rgba[:, :, :3]).permute(2, 0, 1).contiguous()
        png = torchvision.io.encode_png(chw, 9).numpy()

        output_path = pathlib.Path(__file__).parent / self.problem.output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(png)

        if self.runtime.print_status:
            print(f"Wrote {self.problem.width}x{self.problem.height} image to {output_path}")

    def preprocess(self):
        return {self.KERNEL_NAME: self.compiled_code}

    def exec(self):
        if (
            self.runtime.preprocess
            or self.runtime.package
            or self.runtime.bench
            or self.runtime.auto_tunner
        ):
            return super().exec()

        self.init()
        self.generate_data()
        self.create_tensors()
        self.upload_data()
        self.compile_kernels()
        self.create_kernels()

        stream = self.context.stream
        self.dispatch(stream)
        self.context.synchronize(stream)
        self.validate_pipeline()

        if self.runtime.print_status:
            self.print_bin_stats()

        self.save_image()

        #This path bypasses Experiment.exec, so the trace hooks have to be repeated
        if self.runtime.itrace_perfetto:
            self._itrace_perfetto()

        if self.runtime.perfetto_view:
            self._perfetto_view()


if __name__ == "__main__":
    exp = HIP_RasteriserPipeline()
    try:
        exp.main()
    except RuntimeError as ex:
        print(ex)
        raise
