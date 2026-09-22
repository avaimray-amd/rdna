#define __global__ __attribute__((global))
#define __device__ __attribute__((device))

#define TILES_X ((WIDTH + 31) / 32)
#define TILES_Y ((HEIGHT + 31) / 32)
#define NUM_TILES (TILES_X * TILES_Y)

#define SETUP_STRIDE 16

static_assert(NUM_QUEUES > 0 && NUM_QUEUES % 2 == 0, "Rasteriser pipeline requires two triangle queues per workgroup");

#define BIG 1.0e9f

using u32_t = unsigned int;
using i32_t = int;

extern "C"
__global__ __attribute__((amdgpu_flat_work_group_size(32, 32)))
void reuse_pipeline(const u32_t* __restrict__ indices, u32_t* __restrict__ unique_shades, u32_t* __restrict__ connectivity, u32_t* __restrict__ shade_count, u32_t* __restrict__ vertex_dispatch_args)
{
    u32_t lane = __builtin_amdgcn_workitem_id_x();
    u32_t index_position = __builtin_amdgcn_workgroup_id_x() * 30u + lane;
    if (lane >= 30u || index_position >= NUM_TRIANGLES * 3u)
    {
        return;
    }

    u32_t vertex_index = indices[index_position];

    //There are potentially duplicate vertex indices in the vertex_index VGPR, this pass' job is to deal with that

    //Create a bitmask of all the lanes that have the same vertex index as this lane
    u32_t matches = (u32_t)__builtin_amdgcn_wave_match_b32((i32_t)vertex_index, (i32_t)vertex_index);

    //Find the lane that "owns" this index (the first 1 in the bitmask)
    u32_t leader_lane = (u32_t)__builtin_ctz(matches);

    //Make an SGPR bitmask of all the lanes that own an index
    u32_t leaders = __builtin_amdgcn_ballot_w32(lane == leader_lane);

    //Get the unique shade index local to this wave (0, 1, 2, etc.) (will be the same value in all lanes that have duplicate indices)
    u32_t local_shade = (u32_t)__builtin_popcount(leaders & ((1u << leader_lane) - 1u));

    //Get the total number of unique shade indices in this wave
    u32_t local_count = (u32_t)__builtin_popcount(leaders); //number of unique shades in this wave

    //Update global shade count
    u32_t global_shade_base_index = 0;
    if (lane == 0)
    {
        global_shade_base_index = __hip_atomic_fetch_add(shade_count, local_count, __ATOMIC_RELAXED, __HIP_MEMORY_SCOPE_AGENT);

        //Update number of waves for indirect dispatch: (unique shades + 31) / 32
        __hip_atomic_fetch_max(&vertex_dispatch_args[0], (global_shade_base_index + local_count + 31u) / 32u, __ATOMIC_RELAXED, __HIP_MEMORY_SCOPE_AGENT);
    }
    //Global scalar base index for this wave's unique shades
    global_shade_base_index = __builtin_amdgcn_readfirstlane(global_shade_base_index);

    //Global vector index for this lane's unique shade
    u32_t shade = global_shade_base_index + local_shade;
    if (lane == leader_lane)
    {
        unique_shades[shade] = vertex_index;
    }
    //Same duplicativity pattern as the index buffer but references the unique_shades buffer
    //e.g.: index buffer [31, 32, 33, 32, 33, 34] -- connectivity list [49, 50, 51, 50, 51, 52]
    connectivity[index_position] = shade;
    __builtin_amdgcn_fence(__ATOMIC_RELEASE, "agent", "global");
}

extern "C"
__global__ __attribute__((amdgpu_flat_work_group_size(32, 32)))
void vertex_pipeline(const float* __restrict__ input_positions, const u32_t* __restrict__ unique_shades, const u32_t* __restrict__ shade_count, float* __restrict__ shaded_positions)
{
    __builtin_amdgcn_fence(__ATOMIC_ACQUIRE, "agent", "global");
    u32_t shade = __builtin_amdgcn_workgroup_id_x() * 32u + __builtin_amdgcn_workitem_id_x();
    if (shade < *shade_count)
    {
        u32_t vertex_index = unique_shades[shade];
        shaded_positions[shade * 2u] = input_positions[vertex_index * 4u];
        shaded_positions[shade * 2u + 1u] = input_positions[vertex_index * 4u + 1u];
    }
    __builtin_amdgcn_fence(__ATOMIC_RELEASE, "agent", "global");
}

extern "C"
__global__ __attribute__((amdgpu_flat_work_group_size(32, 32)))
void fragment_pipeline(const u32_t* __restrict__ coverage, u32_t* __restrict__ image)
{
    __builtin_amdgcn_fence(__ATOMIC_ACQUIRE, "agent", "global");
    u32_t fragment = __builtin_amdgcn_workgroup_id_x() * 32u + __builtin_amdgcn_workitem_id_x();
    if (fragment < NUM_PIXELS)
    {
        image[fragment] = coverage[fragment] ? 0xFF00FF00u : 0u;
    }
    __builtin_amdgcn_fence(__ATOMIC_RELEASE, "agent", "global");
}

//A wave-wide broadcast that lands in a VGPR. v_bpermute_b32 is a LDS-free crossbar, so
//unlike readlane it never writes an SGPR and never pays the VALU to SALU transfer wait
static __device__ __attribute__((always_inline))
u32_t broadcast(u32_t value, u32_t src_lane)
{
    //ISel folds a wave-uniform selector straight back to v_readlane, so hide it in a VGPR
    asm("" : "+v"(src_lane));
    return __builtin_amdgcn_bpermute_b32(value, src_lane);
}

static __device__ __attribute__((always_inline))
float broadcast(float value, u32_t src_lane)
{
    return __builtin_bit_cast(float, broadcast(__builtin_bit_cast(u32_t, value), src_lane));
}

//An arbitrary cross-lane gather without touching LDS. permlane16_var reads the lane's own
//half of the wave and permlanex16_var reads the other half, both with a per-lane index, so
//the selector's bit 4 is all that decides which of the two answers is the right one
static __device__ __attribute__((always_inline))
u32_t shuffle(u32_t value, u32_t src_lane)
{
    i32_t v = (i32_t)value;
    i32_t sel = (i32_t)(src_lane & 15u);
    i32_t near = __builtin_amdgcn_permlane16_var(v, v, sel, true, false);
    i32_t far = __builtin_amdgcn_permlanex16_var(v, v, sel, true, false);
    u32_t lane = __builtin_amdgcn_workitem_id_x();
    return (u32_t)((((src_lane ^ lane) & 16u) == 0u) ? near : far);
}

static __device__ __attribute__((always_inline))
float shuffle(float value, u32_t src_lane)
{
    return __builtin_bit_cast(float, shuffle(__builtin_bit_cast(u32_t, value), src_lane));
}

static __device__ __attribute__((always_inline))
u32_t wave_exclusive_sum(u32_t value, u32_t lane)
{
    return (u32_t)__builtin_amdgcn_exclusive_scan_sum_u32((i32_t)value, -1, false);
}

struct TriRecord
{
    float m_lo[3], k_lo[3];
    float m_hi[3], k_hi[3];
    u32_t xmin, xmax, ymin, ymax;
};

static __device__ __attribute__((always_inline))
void column_bounds(const TriRecord& s, float px, float& lo, float& hi)
{
    float lo0 = __builtin_fmaf(s.m_lo[0], px, s.k_lo[0]);
    float lo1 = __builtin_fmaf(s.m_lo[1], px, s.k_lo[1]);
    float lo2 = __builtin_fmaf(s.m_lo[2], px, s.k_lo[2]);
    lo = __builtin_fmaxf(lo0, __builtin_fmaxf(lo1, lo2));

    float hi0 = __builtin_fmaf(s.m_hi[0], px, s.k_hi[0]);
    float hi1 = __builtin_fmaf(s.m_hi[1], px, s.k_hi[1]);
    float hi2 = __builtin_fmaf(s.m_hi[2], px, s.k_hi[2]);
    hi = __builtin_fminf(hi0, __builtin_fminf(hi1, hi2));
}

//Calculates if a tile intersects with a tri's edge geometry
//Conservative; should be accompanied with a bounding box check either through
//tile_in_triangle or a tile loop
static __device__ __attribute__((always_inline))
bool touches_tile(const TriRecord& s, u32_t tile_start_x, u32_t tile_start_y)
{
    float xl = (float)tile_start_x;
    float xr = (float)(tile_start_x + 31);

    float lo = -BIG;
    float hi = BIG;
    for (u32_t e = 0; e < 3; ++e)
    {
        float lo_left = __builtin_fmaf(s.m_lo[e], xl, s.k_lo[e]);
        float lo_right = __builtin_fmaf(s.m_lo[e], xr, s.k_lo[e]);
        lo = __builtin_fmaxf(lo, __builtin_fminf(lo_left, lo_right));

        float hi_left = __builtin_fmaf(s.m_hi[e], xl, s.k_hi[e]);
        float hi_right = __builtin_fmaf(s.m_hi[e], xr, s.k_hi[e]);
        hi = __builtin_fminf(hi, __builtin_fmaxf(hi_left, hi_right));
    }

    return lo <= (float)(tile_start_y + 31) && hi >= (float)tile_start_y;
}

//touches_tile with a bounding box pre-check
static __device__ __attribute__((always_inline))
bool tile_in_triangle(const TriRecord& s, u32_t tx, u32_t ty, u32_t tx0, u32_t tx1, u32_t ty0, u32_t ty1)
{
    if (tx < tx0 || tx > tx1 || ty < ty0 || ty > ty1)
    {
        return false;
    }
    return touches_tile(s, tx * 32, ty * 32);
}

extern "C"
__global__ __attribute__((amdgpu_flat_work_group_size(32, 32), amdgpu_waves_per_eu(1, 1), amdgpu_num_vgpr(256), amdgpu_num_sgpr(128)))
void raster_bin_pipeline(u32_t* __restrict__ queue_count, u32_t* __restrict__ queue_list, u32_t* __restrict__ overflow, u32_t* __restrict__ setup, const float* __restrict__ shaded_positions, const u32_t* __restrict__ connectivity)
{
    __builtin_amdgcn_fence(__ATOMIC_ACQUIRE, "agent", "global");
    //Each lane operates per-tri
    u32_t tri = __builtin_amdgcn_workgroup_id_x() * BIN_THREADS_X + __builtin_amdgcn_workitem_id_x();
    if (tri >= NUM_TRIANGLES)
    {
        return;
    }

    //Load the NDC tri data
    u32_t shade0 = connectivity[tri * 3u];
    u32_t shade1 = connectivity[tri * 3u + 1u];
    u32_t shade2 = connectivity[tri * 3u + 2u];
    float tri_v0_x = shaded_positions[shade0 * 2u];
    float tri_v0_y = shaded_positions[shade0 * 2u + 1u];
    float tri_v1_x = shaded_positions[shade1 * 2u];
    float tri_v1_y = shaded_positions[shade1 * 2u + 1u];
    float tri_v2_x = shaded_positions[shade2 * 2u];
    float tri_v2_y = shaded_positions[shade2 * 2u + 1u];

    //Calculate the edge equations
    float A[3], B[3], C[3];
    A[0] = tri_v1_y - tri_v0_y;
    B[0] = tri_v0_x - tri_v1_x;
    C[0] = tri_v1_x * tri_v0_y - tri_v0_x * tri_v1_y;
    A[1] = tri_v2_y - tri_v1_y;
    B[1] = tri_v1_x - tri_v2_x;
    C[1] = tri_v2_x * tri_v1_y - tri_v1_x * tri_v2_y;
    A[2] = tri_v0_y - tri_v2_y;
    B[2] = tri_v2_x - tri_v0_x;
    C[2] = tri_v0_x * tri_v2_y - tri_v2_x * tri_v0_y;

    //Calculate the NDC bounding box
    float tri_v_xmin = __builtin_fminf(tri_v0_x, __builtin_fminf(tri_v1_x, tri_v2_x));
    float tri_v_xmax = __builtin_fmaxf(tri_v0_x, __builtin_fmaxf(tri_v1_x, tri_v2_x));
    float tri_v_ymin = __builtin_fminf(tri_v0_y, __builtin_fminf(tri_v1_y, tri_v2_y));
    float tri_v_ymax = __builtin_fmaxf(tri_v0_y, __builtin_fmaxf(tri_v1_y, tri_v2_y));

    //Calculate the pixel bounding box
    float xmin_bound = __builtin_fmaxf(0.0f, __builtin_floorf(tri_v_xmin * WIDTH));
    float xmax_bound = __builtin_fminf(WIDTH - 1, __builtin_ceilf(tri_v_xmax * WIDTH));
    float ymin_bound = __builtin_fmaxf(0.0f, __builtin_floorf((1.0f - tri_v_ymax) * HEIGHT));
    float ymax_bound = __builtin_fminf(HEIGHT - 1, __builtin_ceilf((1.0f - tri_v_ymin) * HEIGHT));

    //Populate the setup record (TriRecord)
    TriRecord s;

    //Rather than store the edge equations (/increments) and have the consumer test each pixel,
    //each edge is analytically solved for py here, so the consumer gets a row range per column directly
    for (u32_t e = 0; e < 3; ++e)
    {
        //Rewrite e(x,y) = Ax + By + C in pixel space, substituting x = px/(W-1) and
        //y = 1 - py/(H-1). The negated b and the leading B in c are both the y flip.
        float a = A[e] * (1.0f / (WIDTH - 1));
        float b = -B[e] * (1.0f / (HEIGHT - 1));
        float c = B[e] + C[e];

        if (b == 0.0f)
        {
            if (a > 0.0f)
            {
                xmin_bound = __builtin_fmaxf(xmin_bound, __builtin_ceilf(-c / a));
            }
            else if (a < 0.0f)
            {
                xmax_bound = __builtin_fminf(xmax_bound, __builtin_floorf(-c / a));
            }
            else if (c < 0.0f)
            {
                return;
            }
            s.m_lo[e] = 0.0f;
            s.k_lo[e] = -BIG;
            s.m_hi[e] = 0.0f;
            s.k_hi[e] = BIG;
            continue;
        }

        //Inside is a*px + b*py + c >= 0. Hold px fixed and solve for py, giving the
        //line py = slope*px + intercept where this edge crosses the column.
        float slope = -a / b;
        float intercept = -c / b;

        //Dividing by a negative b flips the inequality, so b's sign is what decides
        //whether this edge bounds the column from below or from above.
        bool bounds_from_below = b > 0.0f;

        //Written to both slots so the consumer needs no per-edge branch: the slot this
        //edge does not belong in gets a value that always loses the max/min it feeds.
        s.m_lo[e] = bounds_from_below ? slope : 0.0f;
        s.k_lo[e] = bounds_from_below ? intercept : -BIG;
        s.m_hi[e] = bounds_from_below ? 0.0f : slope;
        s.k_hi[e] = bounds_from_below ? BIG : intercept;
    }
    if (xmin_bound > xmax_bound || ymin_bound > ymax_bound)
    {
        return;
    }
    u32_t xmin = (u32_t)xmin_bound;
    u32_t xmax = (u32_t)xmax_bound;
    u32_t ymin = (u32_t)ymin_bound;
    u32_t ymax = (u32_t)ymax_bound;
    s.xmin = xmin;
    s.xmax = xmax;
    s.ymin = ymin;
    s.ymax = ymax;

    //Place tri record in memory
    u32_t* dst = setup + tri * SETUP_STRIDE;
    for (u32_t e = 0; e < 3; ++e)
    {
        dst[e]     = __builtin_bit_cast(u32_t, s.m_lo[e]);
        dst[3 + e] = __builtin_bit_cast(u32_t, s.k_lo[e]);
        dst[6 + e] = __builtin_bit_cast(u32_t, s.m_hi[e]);
        dst[9 + e] = __builtin_bit_cast(u32_t, s.k_hi[e]);
    }
    dst[12] = xmin;
    dst[13] = xmax;
    dst[14] = ymin;
    dst[15] = ymax;

    //The screen is logically split into tiles, and tiles are owned by consumers.
    //As a producer, our job is to go over all the tiles in the tri's bounding box and
    //if it at least partially intersects with the triangle's edge geometry, send the triangle to
    //the consumer that owns the tile.

    //Calculate bounding box in tile space
    u32_t tx0 = s.xmin / 32;
    u32_t tx1 = s.xmax / 32;
    u32_t ty0 = s.ymin / 32;
    u32_t ty1 = s.ymax / 32;

    //For a large triangle that covers multiple of the same consumer's tile, we only send the tri once.
    //This is a shortcut case to that - consumers are tiled linearly over the grid, so if the tri's
    //bounding box's linear tile tri record is less than there are consumers,
    //we can be sure all tiles have unique owners.
    bool owners_unique = (ty1 - ty0) * TILES_X + (tx1 - tx0) < NUM_QUEUES;

    //Loop through all tiles in the tri's bounding box
    for (u32_t ty = ty0; ty <= ty1; ++ty)
    {
        for (u32_t tx = tx0; tx <= tx1; ++tx)
        {
            //Perform the edge geometry check
            if (!touches_tile(s, tx * 32, ty * 32))
            {
                continue;
            }
            //The edge geometry check passed

            //Calculate the linear tile index
            u32_t tile = ty * TILES_X + tx;

            //Check that this is the first tile owned by this consumer that has passed the check
            //We do this by running the same check for the previous iteration of this consumer's tile
            u32_t previous_tile = tile - NUM_QUEUES;
            u32_t previous_tx = previous_tile % TILES_X;
            u32_t previous_ty = previous_tile / TILES_X;

            //Shortcircuit optimisations:
            //- this can only be a duplicate if owners_unique is false
            //- tile iterates beyond NUM_QUEUES, so if tile < NUM_QUEUES, this is the
            //  first of the consumer's tiles we've seen.
            //Only if both of those fail do we recalculate the previous check
            if (!owners_unique && tile >= NUM_QUEUES && tile_in_triangle(s, previous_tx, previous_ty, tx0, tx1, ty0, ty1))
            {
                continue;
            }

            //This is the first tile of this consumer we've processed for this tri, so send the tri
            //Each consumer has its own queue in memory, so write the tri index into its queue
            u32_t owner = tile % NUM_QUEUES;

            //Needs to be atomic at the GL2 (agent scope) level because all other producers might be
            //writing values into this queue for the tris they're processing simultaneously.
            u32_t slot = __hip_atomic_fetch_add(&queue_count[owner], 1u, __ATOMIC_RELAXED, __HIP_MEMORY_SCOPE_AGENT);
            if (slot < QUEUE_CAPACITY)
            {
                queue_list[owner * QUEUE_CAPACITY + slot] = tri;
            }
            else
            {
                *overflow = 1;
            }
        }
    }
}

static __device__ __attribute__((always_inline))
void rasterise_tile(u32_t* __restrict__ coverage, const TriRecord& s, u32_t tile_x, u32_t tile_y, u32_t lane)
{
    //Each lane handles one column of the 32x32 tile
    //Calculate the global pixel bounds of this tile
    u32_t tile_start_x = tile_x * 32;
    u32_t tile_start_y = tile_y * 32;
    u32_t tile_end_y = tile_start_y + 32;

    //Calculate the column this lane is assigned to. No lane may branch away before the
    //reduction below, because the cross-lane ops read stale registers from inactive lanes
    u32_t pixel_x = tile_start_x + lane;
    bool column_in_box = pixel_x >= s.xmin && pixel_x <= s.xmax && pixel_x < WIDTH;

    //Limit the walk region to the intersection of the tile's bounding box,
    //the tri's bounding box, and the screen height
    u32_t row_start = tile_start_y > s.ymin ? tile_start_y : s.ymin;
    u32_t row_end = tile_end_y < (s.ymax + 1) ? tile_end_y : (s.ymax + 1);
    if (row_end > HEIGHT)
    {
        row_end = HEIGHT;
    }

    //Analytically solve the edge equations to find the y values where the triangle
    //intersects this column, then clamp to the smallest region defined by lo, hi, row_start, and row_end-1
    float lo, hi;
    column_bounds(s, (float)pixel_x, lo, hi);
    lo = __builtin_fmaxf(lo, (float)row_start);
    hi = __builtin_fminf(hi, (float)(row_end - 1));

    //lo and hi are calculated as float, round up/down
    u32_t r_lo = (u32_t)__builtin_ceilf(lo);
    u32_t r_hi = (u32_t)__builtin_floorf(hi);

    //Uncovered lanes take zero groups, so the divergent store loop masks them off by
    //itself. An early return here would cost an exec save and restore on every tile
    bool column_covered = column_in_box && r_lo <= r_hi;
    u32_t rows = column_covered ? (r_hi - r_lo + 1) : 0u;

    //Scaling to a byte offset in 32 bits keeps the address as base + zext(u32), which is
    //the only form that selects the SADDR store and avoids a per-lane 64-bit address add
    u32_t last_offset = (r_hi * WIDTH + pixel_x) * 4u;
    u32_t base_offset = (r_lo * WIDTH + pixel_x) * 4u;

    //Sixteen rows per pass on separate accumulators, so each row's add and clamp issues
    //into the dependency stalls of the other fifteen. A tile is 32 rows, so this is at
    //most two passes
    u32_t offset[16];
    #pragma unroll
    for (u32_t row = 0; row < 16; ++row)
    {
        u32_t candidate = base_offset + row * WIDTH * 4u;
        offset[row] = candidate < last_offset ? candidate : last_offset;
    }

    //A per-lane trip count keeps the loop counter in the VALU. A wave-uniform one costs a
    //readfirstlane, and the SGPR it lands in is then subject to the GFX12 forwarding
    //erratum for the rest of the kernel, so the back edge pays 8 clocks instead of 1
    u32_t groups = (rows + 15u) / 16u;
    for (u32_t group = 0; group < groups; ++group)
    {
        #pragma unroll
        for (u32_t row = 0; row < 16; ++row)
        {
            __hip_atomic_store((u32_t*)((char*)coverage + offset[row]), 1u, __ATOMIC_RELAXED, __HIP_MEMORY_SCOPE_WORKGROUP);
        }

        //Redefining the offsets here makes every update below depend on an instruction the
        //stores precede, so none can be hoisted into the store run. A VALU op between two
        //stores re-arms VA_VDST and costs a second 16 cycle drain
        asm volatile(""
            : "+v"(offset[0]), "+v"(offset[1]), "+v"(offset[2]), "+v"(offset[3]),
              "+v"(offset[4]), "+v"(offset[5]), "+v"(offset[6]), "+v"(offset[7]),
              "+v"(offset[8]), "+v"(offset[9]), "+v"(offset[10]), "+v"(offset[11]),
              "+v"(offset[12]), "+v"(offset[13]), "+v"(offset[14]), "+v"(offset[15]));

        #pragma unroll
        for (u32_t row = 0; row < 16; ++row)
        {
            offset[row] += WIDTH * 64u;
            offset[row] = offset[row] < last_offset ? offset[row] : last_offset;
        }
    }
}

extern "C"
__global__ __attribute__((amdgpu_flat_work_group_size(512, 512), amdgpu_waves_per_eu(8, 8)))
void rasteriser_pipeline(u32_t* __restrict__ coverage, u32_t* __restrict__ queue_count, const u32_t* __restrict__ queue_list, const u32_t* __restrict__ setup)
{
    __builtin_amdgcn_fence(__ATOMIC_ACQUIRE, "agent", "global");
    //Each SIMD has its own triangle queue, shared by the waves running on that SIMD.

    static __attribute__((shared)) u32_t next_triangle[2]; //Starting index of the next set of 32 tris to be processed by a wave; atomically incrementing
    static __attribute__((shared)) u32_t finished_waves[2]; //The number of waves that have finished processing with no more triangle batches left

    //Hierarchy
    u32_t thread = __builtin_amdgcn_workitem_id_x(); //Thread index within the workgroup
    u32_t lane = thread & 31u; //Thread index within the wave
    u32_t simd = __builtin_amdgcn_s_getreg(23u | (8u << 6) | (1u << 11));
    u32_t consumer = simd >> 1;
    u32_t queue = __builtin_amdgcn_workgroup_id_x() * 2u + consumer; //Consumer index (2 SIMD consumers per workgroup, 1 queue each)

    if (thread < 2)
    {
        next_triangle[thread] = 0;
        finished_waves[thread] = 0;
    }
    //Force all subsequent LDS read/writes to wait for previous next_triangle and finished_waves writes to land
    __builtin_amdgcn_fence(__ATOMIC_RELEASE, "workgroup", "local");
    __builtin_amdgcn_s_barrier();
    __builtin_amdgcn_fence(__ATOMIC_ACQUIRE, "workgroup", "local");

    u32_t queue_entry_count = __builtin_elementwise_min(queue_count[queue], (u32_t)QUEUE_CAPACITY);

    //One lane claims 32 triangle entries for the whole wave.
    //Other waves on this SIMD claim their own batches from the same queue.
    u32_t tri_batch_start = 0;
    if (lane == 0)
    {
        tri_batch_start = __hip_atomic_fetch_add(&next_triangle[consumer], 32u, __ATOMIC_RELAXED, __HIP_MEMORY_SCOPE_WORKGROUP);
    }
    tri_batch_start = __builtin_amdgcn_readfirstlane(tri_batch_start);

    //The first run's records are loaded before the loop. Every run after it is loaded a
    //whole iteration before it is used, so only this one pays the round trip up front
    TriRecord tri_record;
    if (tri_batch_start < queue_entry_count)
    {
        //First we must load the global tri ids
        //Local queue index
        u32_t queue_entry = tri_batch_start + lane;

        //Clamping the index rather than branching keeps the load unconditional
        queue_entry = __builtin_elementwise_min(queue_entry, queue_entry_count - 1u);

        //Global tri index
        u32_t tri_id = queue_list[queue * QUEUE_CAPACITY + queue_entry];

        //Now we can use the global tri ids to load the setup data
        const u32_t* source = setup + tri_id * SETUP_STRIDE;
        tri_record = *reinterpret_cast<const TriRecord*>(source);
    }

    //Each lane operates on one triangle from the claimed batch of 32.
    //The next batch is claimed dynamically, not assigned to this wave in advance.
    while (tri_batch_start < queue_entry_count)
    {
        //The next run's ids and records are issued before this run's arithmetic, so the
        //round trip is paid for with work instead of with a wait at the top of the loop
        u32_t next_batch_start = 0;
        if (lane == 0)
        {
            next_batch_start = __hip_atomic_fetch_add(&next_triangle[consumer], 32u, __ATOMIC_RELAXED, __HIP_MEMORY_SCOPE_WORKGROUP);
        }
        next_batch_start = __builtin_amdgcn_readfirstlane(next_batch_start);

        //First we must load the global tri ids
        //Local queue index, one whole run ahead of the one being processed
        u32_t queue_entry = next_batch_start + lane;

        //Clamping the index rather than branching keeps the load unconditional
        queue_entry = __builtin_elementwise_min(queue_entry, queue_entry_count - 1u);

        //Global tri index
        u32_t tri_id = queue_list[queue * QUEUE_CAPACITY + queue_entry];

        //Now we can use the global tri ids to load the setup data
        const u32_t* source = setup + tri_id * SETUP_STRIDE;
        TriRecord next_tri_record = *reinterpret_cast<const TriRecord*>(source);

        //Scalar work
        //Queue index this group of 32 tris starts at is tri_batch_start.
        //Needed because the final group might not be a full 32
        u32_t tri_batch_size = __builtin_fmin(queue_entry_count - tri_batch_start, 32u);

        //Vector work
        //Calculate the first tile belonging to this tri that is owned by this queue
        //and the total number of tiles belonging to this tri that are owned by this queue

        //Pixel space bounding box
        u32_t xmin = tri_record.xmin;
        u32_t xmax = tri_record.xmax;
        u32_t ymin = tri_record.ymin;
        u32_t ymax = tri_record.ymax;

        u32_t lowest_tile = (ymin / 32) * TILES_X + xmin / 32;
        u32_t highest_tile = (ymax / 32) * TILES_X + xmax / 32;
        u32_t stride_offset = (queue + NUM_QUEUES - lowest_tile % NUM_QUEUES) % NUM_QUEUES;
        u32_t tile = lowest_tile + stride_offset;
        u32_t count = tile > highest_tile ? 0u : (highest_tile - tile) / NUM_QUEUES + 1u;

        //Selected rather than branched (can only be false on the final tri batch); an
        //exec mask write here would serialise the otherwise independent batches
        bool lane_in_batch = lane < tri_batch_size;
        u32_t first_tile = lane_in_batch ? tile : 0u;
        u32_t tile_count = lane_in_batch ? count : 0u;

        //Rather than have each lane rasterise its own triangle (which would
        //lead to uneven work distribution for tris of different sizes),
        //We're going to split the *tiles* evenly between the lanes.

        //Create an exclusive prefix sum of the number of tiles each tri covers, e.g.:
        //Lane:  0 | 1 | 2 | 3 | ...
        //Tri:   0 | 1 | 2 | 3 | ...
        //Tiles: 2 | 4 | 1 | 2 | ...
        //Sum:   0 | 2 | 6 | 7 | ...
        u32_t tile_count_exclusive_sum = wave_exclusive_sum(tile_count, lane);

        //Calculate the inclusive prefix sum version of our exclusive prefix sum, e.g.:
        //Tiles: 2 | 4 | 1 | 2 | ...
        //Excl:  0 | 2 | 6 | 7 | ...
        //Incl:  2 | 6 | 7 | 9 | ...
        u32_t tile_count_inclusive_sum = tile_count_exclusive_sum + tile_count;

        //Get the total number of work items (tile, tri) pairs that need to be processed
        //(final lane in the inclusive sum)
        u32_t work_item_count = (u32_t)__builtin_amdgcn_readlane((i32_t)tile_count_inclusive_sum, 31);

        //Each lane loops through its split of the work items (strides by 32)...
        for (u32_t work_item_base = 0; work_item_base < work_item_count; work_item_base += 32u)
        {
            //...and adds lane offset
            u32_t work_item = work_item_base + lane;

            //To rasterise this work item, we need the tri record which is
            //(most likely) owned by a different lane. The owning lane is the one, n, for which
            //this work item index falls between tile_count_exclusive_sum[n] and tile_count_inclusive_sum[n]
            //Rather than having to check both, we can just find the *largest* exclusive sum entry that is <= work_item.
            //We find the owner lane via binary search.
            //
            //Assume the owner_lane is 0
            u32_t owner_lane = 0;
            //Initialise step to 16 (since there are 32 lanes we want to check)
            for (u32_t step = 16; step >= 1; step >>= 1)
            {
                //Calculate the lane to search (first iteration: probe = 0 + 16 = 16)
                u32_t probe = owner_lane + step;

                //Perform the search via shuffle
                if (probe < tri_batch_size && shuffle(tile_count_exclusive_sum, probe) <= work_item)
                {
                    //Take the higher half (next iteration: owner_lane = 16, step = 8, *probe = 24*)
                    owner_lane = probe;
                }
                //Take the lower half (next iteration: owner_lane = 0, step = 8, *probe = 8*)
            }

            //Get the owner's tri record, the first tile owned by this queue belonging to the owner's tri,
            //and the owner's entry in the exclusive sum
            TriRecord owner_tri_record;
            #pragma unroll
            for (u32_t edge = 0; edge < 3; ++edge)
            {
                owner_tri_record.m_lo[edge] = shuffle(tri_record.m_lo[edge], owner_lane);
                owner_tri_record.k_lo[edge] = shuffle(tri_record.k_lo[edge], owner_lane);
                owner_tri_record.m_hi[edge] = shuffle(tri_record.m_hi[edge], owner_lane);
                owner_tri_record.k_hi[edge] = shuffle(tri_record.k_hi[edge], owner_lane);
            }
            owner_tri_record.xmin = shuffle(tri_record.xmin, owner_lane);
            owner_tri_record.xmax = shuffle(tri_record.xmax, owner_lane);
            owner_tri_record.ymin = shuffle(tri_record.ymin, owner_lane);
            owner_tri_record.ymax = shuffle(tri_record.ymax, owner_lane);
            u32_t owner_first_tile = shuffle(first_tile, owner_lane);
            u32_t owner_exclusive_sum_value = shuffle(tile_count_exclusive_sum, owner_lane);

            bool covers_tile = false;
            u32_t tile_x = 0;
            u32_t tile_y = 0;
            if (work_item < work_item_count)
            {
                //Calculate which of the owner's tiles this work_item is (0, 1, 2, 3, etc.)
                u32_t tile_ordinal = work_item - owner_exclusive_sum_value;

                //Calculate the global linear tile index
                u32_t tile = owner_first_tile + tile_ordinal * NUM_QUEUES;

                //Calculate the x and y coordinates in tile-space
                tile_x = tile % TILES_X;
                tile_y = tile / TILES_X;

                //Determine if the tri's edge geometry intersects with this tile
                covers_tile = tile_in_triangle(owner_tri_record, tile_x, tile_y, owner_tri_record.xmin / 32, owner_tri_record.xmax / 32, owner_tri_record.ymin / 32, owner_tri_record.ymax / 32);
            }

            //Each lane has calculated tile_x, tile_y, and covers_tile for a tile.
            //To rasterise a tile, the entire wave is needed.
            //As a scalar wave, go through all the tiles that are covered by their tri,
            //and rasterise them as a vector wave.

            //Get a bitmask of the lanes with tiles that are covered by their tris
            u32_t lanes_with_work = __builtin_amdgcn_ballot_w32(covers_tile);

            //Reading tile n+1's record before rasterising tile n
            //puts the broadcast latency behind work instead of in front of it
            TriRecord raster_tri_record;
            u32_t raster_tile_x;
            u32_t raster_tile_y;
            if (lanes_with_work)
            {
                u32_t source_lane = (u32_t)__builtin_ctz(lanes_with_work);
                #pragma unroll
                for (u32_t edge = 0; edge < 3; ++edge)
                {
                    raster_tri_record.m_lo[edge] = broadcast(owner_tri_record.m_lo[edge], source_lane);
                    raster_tri_record.k_lo[edge] = broadcast(owner_tri_record.k_lo[edge], source_lane);
                    raster_tri_record.m_hi[edge] = broadcast(owner_tri_record.m_hi[edge], source_lane);
                    raster_tri_record.k_hi[edge] = broadcast(owner_tri_record.k_hi[edge], source_lane);
                }
                raster_tri_record.xmin = broadcast(owner_tri_record.xmin, source_lane);
                raster_tri_record.xmax = broadcast(owner_tri_record.xmax, source_lane);
                raster_tri_record.ymin = broadcast(owner_tri_record.ymin, source_lane);
                raster_tri_record.ymax = broadcast(owner_tri_record.ymax, source_lane);
                raster_tile_x = broadcast(tile_x, source_lane);
                raster_tile_y = broadcast(tile_y, source_lane);
            }

            //VALU may cross, SALU may not. Keep the loop test from being hoisted
            //directly behind the broadcasts above.
            __builtin_amdgcn_sched_barrier(0x2 | 0x400);

            //While there are any bits set
            while (lanes_with_work)
            {
                //Unset this bit
                lanes_with_work &= lanes_with_work - 1u;

                //The sentinel bit keeps ctz defined on the final pass; that pass reads
                //lane 31's record and the loop exits before using it
                u32_t next_lane = (u32_t)__builtin_ctz(lanes_with_work | 0x80000000u);

                TriRecord next_raster_tri_record;
                #pragma unroll
                for (u32_t edge = 0; edge < 3; ++edge)
                {
                    next_raster_tri_record.m_lo[edge] = broadcast(owner_tri_record.m_lo[edge], next_lane);
                    next_raster_tri_record.k_lo[edge] = broadcast(owner_tri_record.k_lo[edge], next_lane);
                    next_raster_tri_record.m_hi[edge] = broadcast(owner_tri_record.m_hi[edge], next_lane);
                    next_raster_tri_record.k_hi[edge] = broadcast(owner_tri_record.k_hi[edge], next_lane);
                }
                next_raster_tri_record.xmin = broadcast(owner_tri_record.xmin, next_lane);
                next_raster_tri_record.xmax = broadcast(owner_tri_record.xmax, next_lane);
                next_raster_tri_record.ymin = broadcast(owner_tri_record.ymin, next_lane);
                next_raster_tri_record.ymax = broadcast(owner_tri_record.ymax, next_lane);
                u32_t next_raster_tile_x = broadcast(tile_x, next_lane);
                u32_t next_raster_tile_y = broadcast(tile_y, next_lane);

                __builtin_amdgcn_sched_barrier(0x2 | 0x400);

                //As a vector wave, rasterise this work item
                rasterise_tile(coverage, raster_tri_record, raster_tile_x, raster_tile_y, lane);

                raster_tri_record = next_raster_tri_record;
                raster_tile_x = next_raster_tile_x;
                raster_tile_y = next_raster_tile_y;
            }
        }

        tri_record = next_tri_record;
        tri_batch_start = next_batch_start;
    }
    if (lane == 0)
    {
        //Only the last of this SIMD's eight waves resets the queue count.
        //No final barrier holds the other waves open after they finish.
        if (__hip_atomic_fetch_add(&finished_waves[consumer], 1u, __ATOMIC_ACQ_REL, __HIP_MEMORY_SCOPE_WORKGROUP) == 7u)
        {
            queue_count[queue] = 0;
        }
    }
    __builtin_amdgcn_fence(__ATOMIC_RELEASE, "agent", "global");
}