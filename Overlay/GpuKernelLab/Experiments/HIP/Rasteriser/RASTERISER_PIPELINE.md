# Rasteriser pipeline experiment

`rasteriser_pipeline.py` and `Kernels/rasteriser_pipeline.cpp` are independent
copies of the ILP3 experiment and rasteriser. The connected pipeline uses
five HIP compute kernels in one module. The original ILP3 files are untouched.

The indexed reuse and indirect vertex path passed small gfx1310 Magnus tests on
2026-09-21. Both the original eight-triangle scene and a duplicate-index fixture
passed; the indexed capture and remaining limitations are recorded below.

The copied driver uses the current kernel's 512-thread CU-mode workgroups,
two triangle queues per workgroup, and 32-reference claims. It defaults to
one workgroup and writes `Images/rasteriser_pipeline.png`. Compiler settings
include `-mcumode` and C++20, matching the ILP3 capture runner. The native
scan intrinsic still requires the existing internal gfx1310 compiler path.
This driver does not inherit the capture runner's physical CU mask overrides.

## Connected HIP stages

1. `reuse_pipeline` reads 30 triangle-list indices (10 complete triangles) per
  wave32. Lanes 30 and 31 are inactive, as are missing indices in the final wave.
  `v_wave_match_b32` identifies equal vertex indices; the first matching lane
  becomes the shade leader. A ballot and population counts assign compact
  wave-local shade IDs. One atomic addition per wave reserves a global range;
  leaders write vertex indices into `unique_shades`, and all valid lanes write
  global shade IDs into `connectivity` in the original triangle-list order.
2. `vertex_pipeline` is dispatched indirectly from GPU-generated arguments.
  Each thread reads one compacted vertex index and copies its input `float4` XY
  to `shaded_positions`. Positions use the rasteriser's normalized
  coordinate convention; this minimal pass does not transform or clip them.
3. `raster_bin_pipeline` gathers three shaded positions through connectivity,
  then generates setup records and triangle queues using the existing ILP3 algorithm.
4. `rasteriser_pipeline` writes a binary coverage buffer instead of colour.
5. `fragment_pipeline` reads coverage and writes packed opaque green for covered
  pixels and zero for uncovered pixels.

The shade list, connectivity, total shade count, indirect arguments and shaded
positions reside in global memory, not LDS. Reuse is local to each 10-triangle
block, not across waves. Global range allocation order is nondeterministic;
connectivity retains the original primitive order regardless of allocation order.
The default scene is unchanged: it supplies sequential indices over the original
unique vertices, so it does not exercise reuse hits. `vertices_data` and
`indices_data` are the explicit float4 vertex and uint32 triangle-list inputs.
The capture-local indexed fixture below exercises reuse hits without changing
the default scene. Validation reconstructs the reference geometry from these inputs.

Before every reuse dispatch, GPU commands reset the shade count to zero and the
indirect arguments to `{0, 1, 1}`, then wait for the reset writes. Each reuse wave
atomically maximizes the X dispatch argument with
`ceil((shade_base + local_count) / 32)`. Since the reservations cover a contiguous
range, the final maximum is `ceil(total_shades / 32)` without a conversion pass.
The count reserves storage; it is not a publication signal. Before the indirect
launch, a GPU `RELEASE_MEM` completion signal and `WAIT_REG_MEM` stall the command
processor until reuse has completed, with writeback-only cache flags (no
command-side invalidation). Shader acquire/release fences are unchanged. The signal
buffer is reset with the counters on each dispatch. A shader-side partial flush
alone did not prevent the indirect argument read from happening too early.
There is no overlap between reuse and vertex shading in this version.

The indirect launch uses PyGpuDirect, not the HIP runtime launch API. It checks
wave32/32-thread metadata, rejects vertex scratch and dispatch-pointer requirements
unsupported by this local path, and retains the runner's two-SIMD mask. The vertex
kernel does not use hidden grid dimensions, which the current indirect helper
initializes to zero. The installed builder emits an extra word in the compute
indirect packet. A scoped experiment-local override emits the four-word layout
defined in the packaged Magnus `f32_mec_pm4_packets.h`: header, address low,
address high, dispatch initiator. No shared backend or installed package is modified.

All dispatches are recorded on one GPU stream. Execution barriers and shader
memory fences order the producer/consumer accesses. The CPU does not read back
or upload intermediate data between stages. Shade IDs and shaded positions start
at invalid sentinels; coverage starts at zero. The final image also starts at a sentinel so an omitted stage fails
validation. CPU readbacks happen only after the complete pipeline finishes.

Validation checks wave-local reuse, dense shade IDs, connectivity, rounded-up
indirect arguments, copied positions, binary coverage against independent CPU
edge tests, final pixel colours, overflow, and drained queues. CPU samples
match ILP3's `x/(width-1), 1-y/(height-1)` convention. Exact edge equality
between CPU double precision and GPU fast math is not promised for arbitrary
scenes. No triangle IDs, interpolated attributes, depth, blending or MSAA are
implemented yet. Overlapping triangles contribute to the same binary mask.

## Standalone HLSL references

- `Kernels/rasteriser_pipeline_vertex.hlsl`: `VertexMain` copies one `float4`
  position per thread from `input_positions` at `t0` to `output_positions`
  at `u0`. `VertexConstants` at `b0` supplies `vertex_count`.
- `Kernels/rasteriser_pipeline_fragment.hlsl`: `FragmentMain` writes opaque
  red as a `float4` to `output_colours` at `u0`. `FragmentConstants` at `b0`
  supplies `fragment_count`.

Both are HLSL compute entry points (`cs_6_6`) with 64 threads per workgroup
and bounds checks. They model vertex/fragment work, not native VS/PS stages.
They are retained as references, not loaded or dispatched by the connected
experiment. The executable HIP stages use 32-thread workgroups and adapt the
positions and colour format to the rasteriser's buffers.

## Language interoperability

HLSL and HIP do not have to be the same language. DXC produces DXIL for a
DX12 compute pipeline; HIP produces a code object for its runtime. Combining
them later needs compatible shared resources and GPU synchronization, or
explicit copies between the runtimes. The local HIP headers declare D3D12
resource/heap and fence handle types, but interoperability through this
project's Python wrappers and simulator has not been implemented or tested.
The connected all-HIP path avoids that additional integration work.

## Verified Indexed Smoke Test (2026-09-21)

Both runs use Magnus AM, gfx1310, 256x256, one repetition, two enabled SIMDs,
and the unchanged kernel source including the user's explanatory comments.

- `.om_out/pipeline_reuse_8tri_fence_20260921_125232/`: eight original triangles,
  24 input indices and 24 shades, indirect arguments `{1, 1, 1}`, 95 covered pixels.
- `.om_out/pipeline_reuse_indexed12tri_20260921/capture/`: 12 triangles,
  36 input indices, 30 shades, indirect arguments `{1, 1, 1}`, 70 covered pixels.
  The first reuse wave handles 10 triangles with 24 unique indices; the second
  handles two triangles with six. Those six indices also occur in the first wave,
  confirming that reuse remains wave-local. Six duplicate references are removed.

The indexed fixture is saved as `indexed_input.npz`; the sibling `run_indexed.py`
constructs it with a scoped override of data generation. Default production data
generation is unchanged. Both runs pass reuse, dense shade IDs, connectivity,
indirect argument, vertex XY, CPU coverage and green image checks, with zero
overflow and drained queues. No intermediate CPU readback was added.

The writeback-only command flags were subsequently verified with the same indexed
fixture in `.om_out/pipeline_reuse_wbonly_20260921/result/`. All validation checks
passed; the image, kernel source and code-object hashes match the preceding run.
Instruction tracing and Perfetto conversion were disabled for this rerun, and no
trace was generated. The earlier Perfetto capture retains the invalidating flags.

The indexed `HIP_RasteriserPipeline.pftrace` imports with zero errors and contains
2,068 complete waves: two reuse, one indirectly launched vertex, one bin, sixteen
raster and 2,048 fullscreen fragment waves. Placement is SE0/SA0/WGP0 SIMD0/2.
The captured reuse ISA contains `v_wave_match_b32`. The trace converter omits
dependency arrows for unsupported `kmcnt`, `loadcnt`, `storecnt`, and `dscnt`;
wave and instruction events remain available. Timestamps are model ticks.

The first baseline attempt in `.om_out/pipeline_reuse_8tri_20260921_124343/`
failed vertex validation: reuse/count checks passed, but no vertex wave launched.
Adding the explicit GPU completion signal/wait in the experiment driver fixed
the same case. No kernel, simulator, or backend package source was changed.
The missing Python dependency `ml_dtypes==0.5.4` was installed from the repository's
pinned requirements before execution. These small cases establish correctness,
not a performance improvement or large-workload validation.

## Vertical-Edge Fix (2026-09-21)

The two-triangle square exposed division by zero when setup solved a vertical
edge for a row limit. Setup now handles `b == 0` by tightening the column bounds:
`ceil(-c/a)` for a lower bound or `floor(-c/a)` for an upper bound. That edge gets
neutral row limits. A zero-coefficient edge is neutral unless its constant makes
the inside test impossible. Bounds remain floating point until empty bounds have
been rejected, before unsigned conversion. The 64-byte setup record, raster loop,
reuse algorithm, and original ILP3/ILP4 kernels are unchanged.

The exact failing square passed on Magnus in
`.om_out/pipeline_reuse_square_fixed_20260921_140238/`: four vertices, indices
`[0, 1, 2, 2, 1, 3]`, four shades and one indirect vertex workgroup. Coverage and
image match the CPU reference exactly: 36,864 pixels (192x192), with none of the
previous 193 extra pixels in column 224. The executed code object's disassembly
is saved as `rasteriser_pipeline_gfx1310_asm.pp` in that run folder.

The earlier non-vertical 12-triangle scene was rerun in
`.om_out/pipeline_reuse_7x_verticalfix_20260921_140841/`. All checks passed and its
3,363-pixel image is byte-identical to the previous result. Both regression runs
use writeback-only command flags and have tracing disabled. These checks do not
establish full graphics rasterisation rules or general degenerate-triangle support.

## Capture Runner and Historical Smoke Test

```powershell
& .\.venv\Scripts\python.exe Experiments\HIP\Rasteriser\run_pipeline_model.py --output-dir .om_out\pipeline_new
```

Use the existing internal compiler environment (`CLANG_EXE`, `PYTHONPATH`
and `ROCM_PATH`). By default the output directory must not exist. Add
`--overwrite` to reuse a capture subfolder inside `GpuKernelLab/.om_out`.
The F5 launch configuration enables this option: reuse the same folder name
to replace the previous code object, source snapshot, result and trace.
Old trace inputs are cleared before execution; unrelated files are preserved.
The image is always saved to `Experiments/HIP/Rasteriser/Images/rasteriser_pipeline.png`
and overwritten on each successful run, independently of the capture folder.
Defaults are 256x256,
eight triangles, seed 42, one repetition and one 512-thread raster workgroup
across two SIMDs. The runner reuses the established single-CU mask and TCore
memory configuration; it does not modify the simulator or the GPUD backend.

The historical four-stage, red-output capture
`.om_out/rasteriser_pipeline_8tri_2simds_verified_20260916/` passed all
intermediate and final checks: 95 covered pixels, 65,441 uncovered pixels,
zero overflow and both queues drained. Its PNG also matches the previous
eight-triangle reference (SHA256
`f571ae5453ec2dc4fdea4c5aa4c88074ddf0107e7adbf3c0b733145210f2757b`).
The trace imports without errors and contains 2,066 completed waves on
SE0/SA0/WGP0 SIMD0 and SIMD2: one vertex wave, one bin wave, sixteen raster
waves, and 2,048 fragment waves. Eight triangles do not provide enough
32-reference batches to keep all raster waves doing useful work. This test
verifies the pipeline connections, not load balancing or performance.

## Historical Compilation Checks

Both HLSL references compile independently with DXC using `-T cs_6_6
-HV 2021 -O3 -WX` and their respective entry-point names. The connected HIP
module before indexed reuse compiled for gfx1310 with all four entry points and
no scratch load/store instructions. Only the raster pass used LDS (16 bytes of
counters). The five-stage implementation has since compiled and executed in the
small indexed tests above; these historical resource figures are not a fresh
resource audit of the five-stage module.

## Independent Work-Graph Smoke Test

`../../DX12/Rasteriser/work_graph_smoke.py` runs
`../../DX12/Rasteriser/Kernels/work_graph_smoke.hlsl` as a D3D12 work graph.
It does not change or launch the HIP pipeline above.

From the GpuKernelLab root, with `PYTHONPATH` pointing to that root and
`DX12_SC_PATH` / `DX12_SDK_PATH` pointing to the DXC and Agility SDK directories:

```powershell
& .\.venv\Scripts\python.exe Experiments/DX12/Rasteriser/work_graph_smoke.py --debug_layer --gpu_validation
```

The `Vertex` broadcasting node uses 32-thread groups and synthesizes distinct
float4 positions for consecutive vertices in a unique-vertex triangle list.
Globally coherent position writes precede atomic increments of per-triangle
completion counters. The last arrival gathers all three positions and emits a
`TriangleRecord` to the `Bin` thread-launch node. Record operations are group
uniform, with zero records requested by threads that do not complete a triangle.
There is no global vertex-pass barrier or CPU readback between nodes.

`Bin` is a validation stub, not the existing binning algorithm: it writes the
received positions, invocation count, and observed wave lane information.
Readback checks exact positions, three vertex completions per triangle, and
exactly one consumer invocation per triangle. This proves GPU-generated node
handoff, not concurrent stage execution, scheduling latency, or performance.

Verified on 2026-09-17 on RX 9070 XT (`gfx1201`), Work Graphs Tier 1.0:

| Triangles | Vertices | Bin invocations by active-lane count |
| --- | --- | --- |
| 1 | 3 | 1 invocation observed 1 active lane |
| 11 | 33 | 11 invocations observed 11 active lanes |
| 65 | 195 | 42 observed 21, 22 observed 22, 1 observed 1 |
| 1024 | 3072 | All 1024 observed 32 active lanes |

The 11-triangle case crosses the first vertex-group boundary. All cases passed;
the 1, 11, and 1024 cases also used GPU-based validation. Packing observations
are from those runs, not scheduling guarantees or measured wave-launch counts.
Default output is `.cc_out/work_graph_smoke/{work_graph_smoke.dxil,result.json}`;
`--num_triangles` and `--output_dir` select other cases. Work-graph COM bindings
remain local to the test, reusing the existing DXR state-object helper. No
shared bindings, installed packages, or simulator sources were modified. Magnus
work-graph execution is not tested by this physical-GPU experiment.