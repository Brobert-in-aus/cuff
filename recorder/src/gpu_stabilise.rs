//! Stabilisation's per-frame pass on the GPU (shaders/stabilise.comp): it
//! rewrites the lookup grid for this frame's rotation, in the same submission
//! as the conversion and just before it. The CPU only writes the rotation.

use anyhow::{Context, Result};
use ash::vk;

use super::Gpu;
use crate::lut::StableConstants;

const SHADER: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/stabilise.spv"));
const GROUP: u32 = 64; // local_size_x in stabilise.comp

/// The shader's Params block (std430: rows of the rotation, then constants).
#[repr(C)]
#[derive(Clone, Copy)]
struct Params {
    rot: [[f32; 4]; 3],
    view: [f32; 4],
    scale: [f32; 4],
    info: [u32; 4],
}

pub(super) struct StabilisePass {
    shader: vk::ShaderModule,
    set_layout: vk::DescriptorSetLayout,
    pipeline_layout: vk::PipelineLayout,
    pipeline: vk::Pipeline,
    pool: vk::DescriptorPool,
    cmd: vk::CommandBuffer,
    /// Rays, inverse lens map, params.
    buffers: Vec<(vk::Buffer, vk::DeviceMemory)>,
    params: Params,
    params_ptr: *mut Params,
}

impl Gpu {
    /// Sets up the per-frame pass. After this, `convert` rebuilds the grid
    /// for whatever `set_rotation` last set (identity to start with).
    pub fn enable_stabilise(&mut self, rays: &[[f32; 2]], inverse: &[[f32; 2]], k: &StableConstants) -> Result<()> {
        let d = &self.device;
        let (rays_buf, rays_mem) = self.host_buffer(rays)?;
        let (inv_buf, inv_mem) = self.host_buffer(inverse)?;
        let params = Params {
            rot: [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]],
            view: [k.roll_sin, k.roll_cos, k.l, k.t],
            scale: [k.sx, k.sy, k.eye_offset, 0.0],
            info: [k.count, k.inverse_size, k.per_channel as u32, 0],
        };
        let (params_buf, params_mem) = self.host_buffer(std::slice::from_ref(&params))?;
        unsafe {
            let params_ptr = d.map_memory(params_mem, 0, vk::WHOLE_SIZE, vk::MemoryMapFlags::empty())?.cast::<Params>();
            params_ptr.write(params);

            let code = ash::util::read_spv(&mut std::io::Cursor::new(SHADER))?;
            let shader = d.create_shader_module(&vk::ShaderModuleCreateInfo::default().code(&code), None)?;
            let bindings: Vec<_> = (0..4)
                .map(|b| {
                    vk::DescriptorSetLayoutBinding::default()
                        .binding(b)
                        .descriptor_type(vk::DescriptorType::STORAGE_BUFFER)
                        .descriptor_count(1)
                        .stage_flags(vk::ShaderStageFlags::COMPUTE)
                })
                .collect();
            let set_layout =
                d.create_descriptor_set_layout(&vk::DescriptorSetLayoutCreateInfo::default().bindings(&bindings), None)?;
            let set_layouts = [set_layout];
            let pipeline_layout =
                d.create_pipeline_layout(&vk::PipelineLayoutCreateInfo::default().set_layouts(&set_layouts), None)?;
            let stage = vk::PipelineShaderStageCreateInfo::default()
                .stage(vk::ShaderStageFlags::COMPUTE)
                .module(shader)
                .name(c"main");
            let pipeline = d
                .create_compute_pipelines(
                    vk::PipelineCache::null(),
                    &[vk::ComputePipelineCreateInfo::default().stage(stage).layout(pipeline_layout)],
                    None,
                )
                .map_err(|(_, e)| e)?[0];

            let sizes = [vk::DescriptorPoolSize::default().ty(vk::DescriptorType::STORAGE_BUFFER).descriptor_count(4)];
            let pool = d.create_descriptor_pool(&vk::DescriptorPoolCreateInfo::default().max_sets(1).pool_sizes(&sizes), None)?;
            let set = d.allocate_descriptor_sets(
                &vk::DescriptorSetAllocateInfo::default().descriptor_pool(pool).set_layouts(&set_layouts),
            )?[0];
            let infos: Vec<[vk::DescriptorBufferInfo; 1]> = [rays_buf, inv_buf, self.lut_buffer, params_buf]
                .iter()
                .map(|&b| [vk::DescriptorBufferInfo::default().buffer(b).range(vk::WHOLE_SIZE)])
                .collect();
            let writes: Vec<_> = infos
                .iter()
                .enumerate()
                .map(|(i, info)| {
                    vk::WriteDescriptorSet::default()
                        .dst_set(set)
                        .dst_binding(i as u32)
                        .descriptor_type(vk::DescriptorType::STORAGE_BUFFER)
                        .buffer_info(info)
                })
                .collect();
            d.update_descriptor_sets(&writes, &[]);

            // Recorded once: dispatch, then make the grid visible to the conversion.
            let cmd = d.allocate_command_buffers(
                &vk::CommandBufferAllocateInfo::default()
                    .command_pool(self.cmd_pool)
                    .level(vk::CommandBufferLevel::PRIMARY)
                    .command_buffer_count(1),
            )?[0];
            d.begin_command_buffer(cmd, &vk::CommandBufferBeginInfo::default())?;
            d.cmd_bind_pipeline(cmd, vk::PipelineBindPoint::COMPUTE, pipeline);
            d.cmd_bind_descriptor_sets(cmd, vk::PipelineBindPoint::COMPUTE, pipeline_layout, 0, &[set], &[]);
            d.cmd_dispatch(cmd, k.count.div_ceil(GROUP), 1, 1);
            let barrier = vk::BufferMemoryBarrier::default()
                .src_access_mask(vk::AccessFlags::SHADER_WRITE)
                .dst_access_mask(vk::AccessFlags::SHADER_READ)
                .src_queue_family_index(vk::QUEUE_FAMILY_IGNORED)
                .dst_queue_family_index(vk::QUEUE_FAMILY_IGNORED)
                .buffer(self.lut_buffer)
                .size(vk::WHOLE_SIZE);
            d.cmd_pipeline_barrier(
                cmd,
                vk::PipelineStageFlags::COMPUTE_SHADER,
                vk::PipelineStageFlags::COMPUTE_SHADER,
                vk::DependencyFlags::empty(),
                &[],
                &[barrier],
                &[],
            );
            d.end_command_buffer(cmd)?;

            self.stabilise = Some(StabilisePass {
                shader,
                set_layout,
                pipeline_layout,
                pipeline,
                pool,
                cmd,
                buffers: vec![(rays_buf, rays_mem), (inv_buf, inv_mem), (params_buf, params_mem)],
                params,
                params_ptr,
            });
        }
        Ok(())
    }

    /// This frame's correction (video frame -> head frame), for the next `convert`.
    pub fn set_rotation(&mut self, rot: &[[f64; 3]; 3]) {
        if let Some(s) = &mut self.stabilise {
            for (row, r) in s.params.rot.iter_mut().zip(rot) {
                *row = [r[0] as f32, r[1] as f32, r[2] as f32, 0.0];
            }
            // Host coherent, and the next submit makes host writes visible.
            unsafe { s.params_ptr.write(s.params) };
        }
    }

    /// The command buffer that rebuilds the grid, to submit before a conversion.
    pub(super) fn stabilise_cmd(&self) -> Option<vk::CommandBuffer> {
        self.stabilise.as_ref().map(|s| s.cmd)
    }

    /// Runs the pass on its own and reads the grid back, for the startup check.
    pub fn stabilised_grid(&self, len: usize) -> Result<Vec<[f32; 2]>> {
        let cmd = self.stabilise_cmd().context("stabilisation isn't set up")?;
        let d = &self.device;
        unsafe {
            // Not `submit`: that reads the conversion's timestamps, which this doesn't write.
            if let Err(e) = d.queue_submit(self.queue, &[vk::SubmitInfo::default().command_buffers(&[cmd])], self.fence) {
                let _ = d.device_wait_idle();
                anyhow::bail!("submitting GPU work: {e}");
            }
            self.wait_submitted()?;
            let ptr = d.map_memory(self.lut_memory, 0, vk::WHOLE_SIZE, vk::MemoryMapFlags::empty())?.cast::<[f32; 2]>();
            let out = std::slice::from_raw_parts(ptr, len).to_vec();
            d.unmap_memory(self.lut_memory);
            Ok(out)
        }
    }

    /// A host-visible, coherent storage buffer holding `data`.
    fn host_buffer<T: Copy>(&self, data: &[T]) -> Result<(vk::Buffer, vk::DeviceMemory)> {
        let size = std::mem::size_of_val(data) as u64;
        let d = &self.device;
        unsafe {
            let buffer =
                d.create_buffer(&vk::BufferCreateInfo::default().size(size).usage(vk::BufferUsageFlags::STORAGE_BUFFER), None)?;
            let req = d.get_buffer_memory_requirements(buffer);
            let ty = self
                .find_memory_type(req.memory_type_bits, vk::MemoryPropertyFlags::HOST_VISIBLE | vk::MemoryPropertyFlags::HOST_COHERENT)
                .context("no host visible memory for stabilisation")?;
            let memory =
                d.allocate_memory(&vk::MemoryAllocateInfo::default().allocation_size(req.size).memory_type_index(ty), None)?;
            d.bind_buffer_memory(buffer, memory, 0)?;
            let ptr = d.map_memory(memory, 0, vk::WHOLE_SIZE, vk::MemoryMapFlags::empty())?;
            std::ptr::copy_nonoverlapping(data.as_ptr().cast::<u8>(), ptr.cast::<u8>(), size as usize);
            d.unmap_memory(memory);
            Ok((buffer, memory))
        }
    }

    pub(super) fn destroy_stabilise(&mut self) {
        let Some(s) = self.stabilise.take() else { return };
        let d = &self.device;
        unsafe {
            for (buffer, memory) in s.buffers {
                d.destroy_buffer(buffer, None);
                d.free_memory(memory, None);
            }
            d.destroy_descriptor_pool(s.pool, None);
            d.destroy_pipeline(s.pipeline, None);
            d.destroy_pipeline_layout(s.pipeline_layout, None);
            d.destroy_descriptor_set_layout(s.set_layout, None);
            d.destroy_shader_module(s.shader, None);
        }
    }
}
