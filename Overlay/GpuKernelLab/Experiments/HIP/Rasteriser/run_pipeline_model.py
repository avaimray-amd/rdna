import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
from unittest.mock import patch

from Experiments.GPUD.gpud_stream import GPUDStream
from Experiments.HIP.Rasteriser.rasteriser_pipeline import HIP_RasteriserPipeline
from Experiments.HIP.Rasteriser.run_ilp3_simd_model import dispatch_all_cus, small_memory_config


def prepare_output_directory(output, overwrite=False):
    output = output.resolve()
    if overwrite:
        capture_root = (Path(__file__).resolve().parents[3] / ".om_out").resolve()
        if output == capture_root or not output.is_relative_to(capture_root):
            raise ValueError("--overwrite requires a capture subfolder inside GpuKernelLab/.om_out")
    output.mkdir(parents=True, exist_ok=overwrite)
    if overwrite:
        for pattern in (
            "rasteriser_pipeline.cpp", "rasteriser_pipeline.co", "rasteriser_pipeline.png",
            "result.json", "HIP_RasteriserPipeline.pftrace", "csim.log",
            "se*sa*itrace*.mon", "perf_counters*.csv",
        ):
            for artifact in output.glob(pattern):
                if artifact.is_file() or artifact.is_symlink():
                    artifact.unlink()
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true", help="Reuse a capture folder under .om_out and replace previous run outputs")
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=256)
    parser.add_argument("--num-triangles", type=int, default=8)
    parser.add_argument(
        "--model-package", type=Path,
        default=Path(__file__).resolve().parents[4] / "one_model-magnus-13.0.8982267.21415-src/build/package",
    )
    options = parser.parse_args()
    model_package = options.model_package.resolve()
    if not (model_package / "runtime").is_dir() or not (model_package / "bin/csimulate_shared.dll").is_file():
        raise FileNotFoundError(f"Incomplete Magnus runtime package: {model_package}")
    output = prepare_output_directory(options.output_dir, options.overwrite)
    image_output = Path(__file__).resolve().parent / "Images/rasteriser_pipeline.png"
    source = Path(__file__).parent / "Kernels/rasteriser_pipeline.cpp"
    shutil.copy2(source, output / source.name)
    os.chdir(output)
    if shutil.which("grep") is None:
        grep_dir = Path("C:/Program Files/Git/usr/bin")
        if not (grep_dir / "grep.exe").is_file():
            raise FileNotFoundError("Trace conversion requires grep.exe")
        os.environ["PATH"] = str(grep_dir) + os.pathsep + os.environ["PATH"]

    original_exec = HIP_RasteriserPipeline.exec
    original_compile = HIP_RasteriserPipeline.compile_kernels

    def compile_and_save(self):
        original_compile(self)
        (output / "rasteriser_pipeline.co").write_bytes(self.compiled_code)

    def execute(self):
        if self.context.arch_name != "gfx1310" or self.num_workgroups != 1:
            raise ValueError("This smoke test requires gfx1310 and one workgroup")
        self.context.stream.raster_enabled_simds = 2
        self.context.stream.raster_launch_resources = []
        original_exec(self)
        for resource in self.context.stream.raster_launch_resources:
            resource.destroy()
        self.context.stream.raster_launch_resources.clear()
        if not self.validation_result["covered_pixels"] or not self.validation_result["uncovered_pixels"]:
            raise RuntimeError("Smoke test needs both covered and uncovered pixels")
        result = {
            "width": self.problem.width,
            "height": self.problem.height,
            "triangles": self.problem.num_triangles,
            "seed": self.runtime.seed,
            "repeats": 1,
            "enabled_simds": 2,
            "raster_workgroups": self.num_workgroups,
            "raster_threads_per_workgroup": self.RASTER_THREADS_X,
            "reuse_triangles_per_wave": self.REUSE_TRIANGLES_PER_WAVE,
            "reuse_scope": "wave_local",
            "reuse_storage": "global_memory",
            "stages": [self.REUSE_KERNEL_NAME, self.VERTEX_KERNEL_NAME, self.BIN_KERNEL_NAME,
                       self.KERNEL_NAME, self.FRAGMENT_KERNEL_NAME],
            "stage_language": "HIP",
            "cpu_readback_between_stages": False,
            **self.validation_result,
            "image_sha256": hashlib.sha256(image_output.read_bytes()).hexdigest(),
            "source_sha256": hashlib.sha256((output / source.name).read_bytes()).hexdigest(),
            "code_sha256": hashlib.sha256(self.compiled_code).hexdigest(),
        }
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="ascii")
        print(json.dumps(result, indent=2), flush=True)

    with patch.object(GPUDStream, "dispatch", dispatch_all_cus), patch(
        "Experiments.GPUD.gpud_context.TCoreConfig", small_memory_config
    ), patch.object(HIP_RasteriserPipeline, "compile_kernels", compile_and_save), patch.object(
        HIP_RasteriserPipeline, "exec", execute
    ):
        HIP_RasteriserPipeline().main([
            "--gpud_context", "--gpud_backend=tcore", "--one_model", "--om_model=AM",
            "--om_gpu_name=magnus",
            f"--om_package={model_package}",
            "--cc_gpu_arch=gfx1310", "--workgroups=1", "--bin_threads=32",
            f"--num_triangles={options.num_triangles}", f"--width={options.width}",
            f"--height={options.height}", "--records_in_flight=1", "--seed=42",
            f"--output={image_output}", "--om_itrace", "--itrace_perfetto",
        ])


if __name__ == "__main__":
    main()