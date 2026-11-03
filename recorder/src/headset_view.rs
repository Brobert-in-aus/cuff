//! SteamVR's headset view: the flat picture SteamVR renders from the game's
//! eye images, before any lens distortion. It's what Steam's own recorder
//! saves, and it's sharper than un-warping the panel because it's only
//! resampled once.
//!
//! On the Frame it's fixed by SteamVR: 1920x1080 of the left eye. Asking for
//! another size, eye or crop through IVRHeadsetView is silently ignored.

use std::ffi::{c_char, c_void, CStr, CString};

use anyhow::{bail, Context, Result};
use ash::vk;
use ash::vk::Handle;
use cuff_recorder::openvr::OpenVr;

use crate::gpu::{Extensions, NativeHandles};

// Interface versions and slots in their function tables (openvr_capi.h).
// Like openvr.rs, each interface lists the versions whose layout is known
// for the calls used here, newest SteamVR's first, and takes the first one
// SteamVR offers, so a SteamVR that drops one version still works.
//
// IVRCompositor: the Vulkan extension calls moved down a slot in each of 028
// and 029 (a call was added before them each time), so each version has its
// own slots. Measured on the Frame's SteamVR 2.18.4 by calling each one
// (the device call at the instance call's slot fails in the Vulkan loader),
// and 027's match the openvr_capi.h SteamVR ships in its tools folder.
const COMPOSITORS: &[(&str, usize, usize)] =
    &[("IVRCompositor_029", 41, 42), ("IVRCompositor_028", 40, 41), ("IVRCompositor_027", 39, 40)];
// IVROverlay: FindOverlay has been slot 0 in every version; 029, 028, 027
// and 024 all found the headset view on the Frame.
const OVERLAYS: &[&str] = &["IVROverlay_028", "IVROverlay_029", "IVROverlay_027"];
const SLOT_FIND_OVERLAY: usize = 0;
// IVROverlayView and IVRHeadsetView have no fallback: 003 and 001 are the
// only versions with a published layout (SteamVR still offers
// IVROverlayView_001 and _002, but nothing says what's in them).
const OVERLAY_VIEW: &str = "IVROverlayView_003";
const SLOT_ACQUIRE_VIEW: usize = 0;
const SLOT_RELEASE_VIEW: usize = 1;
const HEADSET_VIEW: &str = "IVRHeadsetView_001";
const SLOT_GET_SIZE: usize = 1;
const SLOT_GET_MODE: usize = 3;

const DEVICE_VULKAN: i32 = 1;
const OVERLAY_KEY: &CStr = c"system.HeadsetView";

#[repr(C)]
struct VulkanDevice {
    instance: *mut c_void,
    device: *mut c_void,
    physical_device: *mut c_void,
    queue: *mut c_void,
    queue_family: u32,
}

#[repr(C)]
struct NativeDevice {
    handle: *mut c_void,
    kind: i32,
}

#[repr(C)]
struct Texture {
    handle: *mut c_void,
    kind: i32,
    color_space: i32,
}

#[repr(C)]
struct Bounds {
    u_min: f32,
    v_min: f32,
    u_max: f32,
    v_max: f32,
}

#[repr(C)]
struct OverlayView {
    overlay: u64,
    texture: Texture,
    bounds: Bounds,
}

#[repr(C)]
struct VulkanTextureData {
    image: u64,
    device: *mut c_void,
    physical_device: *mut c_void,
    instance: *mut c_void,
    queue: *mut c_void,
    queue_family: u32,
    width: u32,
    height: u32,
    format: u32,
    sample_count: u32,
}

/// One frame of the headset view, on our device.
pub struct Frame {
    pub image: vk::Image,
    pub origin: (i32, i32),
    pub width: u32,
    pub height: u32,
}

unsafe fn slot<T: Copy>(table: *const *const c_void, index: usize) -> T {
    std::mem::transmute_copy(&*table.add(index))
}

fn extension_list(raw: &[u8]) -> Vec<CString> {
    let text = CStr::from_bytes_until_nul(raw).map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    text.split_whitespace().filter_map(|e| CString::new(e).ok()).collect()
}

/// The first of `versions` SteamVR offers, with its function table.
fn first_interface<'a, T: Copy>(vr: &OpenVr, versions: &'a [T], name: impl Fn(&T) -> &'a str) -> Option<(T, *const *const c_void)> {
    let found = versions.iter().find_map(|v| vr.interface(name(v)).map(|table| (*v, table)));
    if let Some((v, _)) = &found {
        log::debug!("using {}", name(v));
    }
    found
}

/// The Vulkan extensions SteamVR needs to share textures with our device.
pub fn vulkan_extensions(vr: &OpenVr) -> Result<Extensions> {
    let ((_, slot_instance, slot_device), table) =
        first_interface(vr, COMPOSITORS, |v| v.0).context("SteamVR doesn't offer a known IVRCompositor version")?;
    let table = table as usize;
    let mut buf = vec![0u8; 4096];
    let instance = unsafe {
        let f: unsafe extern "C" fn(*mut c_char, u32) -> u32 = slot(table as *const *const c_void, slot_instance);
        f(buf.as_mut_ptr().cast(), buf.len() as u32);
        extension_list(&buf)
    };
    Ok(Extensions {
        instance,
        device: Box::new(move |pdev| unsafe {
            let mut buf = vec![0u8; 4096];
            let f: unsafe extern "C" fn(*mut c_void, *mut c_char, u32) -> u32 =
                slot(table as *const *const c_void, slot_device);
            f(pdev.as_raw() as *mut c_void, buf.as_mut_ptr().cast(), buf.len() as u32);
            extension_list(&buf)
        }),
    })
}

/// Size and eye (0 left, 1 right) SteamVR renders the headset view at.
pub fn current(vr: &OpenVr) -> Result<((u32, u32), usize)> {
    let table = vr.interface(HEADSET_VIEW).context("SteamVR doesn't offer IVRHeadsetView_001")?;
    unsafe {
        let (mut w, mut h) = (0, 0);
        let get_size: unsafe extern "C" fn(*mut u32, *mut u32) = slot(table, SLOT_GET_SIZE);
        get_size(&mut w, &mut h);
        let get_mode: unsafe extern "C" fn() -> i32 = slot(table, SLOT_GET_MODE);
        Ok(((w, h), get_mode().max(0) as usize))
    }
}

pub struct HeadsetView {
    view_if: *const *const c_void,
    handle: u64,
    view: Box<OverlayView>,
    // Pointed at by `native`, which SteamVR reads on every acquire.
    _device: Box<VulkanDevice>,
    native: Box<NativeDevice>,
    acquired: bool,
}

impl HeadsetView {
    pub fn new(vr: &OpenVr, gpu: &NativeHandles) -> Result<Self> {
        let (_, overlay) = first_interface(vr, OVERLAYS, |v| *v).context("SteamVR doesn't offer a known IVROverlay version")?;
        let view_if = vr.interface(OVERLAY_VIEW).context("SteamVR doesn't offer IVROverlayView_003")?;

        let mut handle = 0u64;
        let err = unsafe {
            let f: unsafe extern "C" fn(*const c_char, *mut u64) -> i32 = slot(overlay, SLOT_FIND_OVERLAY);
            f(OVERLAY_KEY.as_ptr(), &mut handle)
        };
        if err != 0 {
            bail!("SteamVR's headset view isn't there (overlay error {err})");
        }

        let mut device = Box::new(VulkanDevice {
            instance: gpu.instance.as_raw() as *mut c_void,
            device: gpu.device.as_raw() as *mut c_void,
            physical_device: gpu.physical_device.as_raw() as *mut c_void,
            queue: gpu.queue.as_raw() as *mut c_void,
            queue_family: gpu.queue_family,
        });
        let native = Box::new(NativeDevice { handle: (&mut *device as *mut VulkanDevice).cast(), kind: DEVICE_VULKAN });
        Ok(Self {
            view_if,
            handle,
            view: Box::new(unsafe { std::mem::zeroed() }),
            _device: device,
            native,
            acquired: false,
        })
    }

    /// The newest frame, or None while SteamVR hasn't drawn one yet. Call
    /// `release` once the frame is copied: SteamVR keeps a record for every
    /// acquire until it's released, so acquiring per frame without
    /// releasing grows vrcompositor by gigabytes a day.
    pub fn acquire(&mut self) -> Result<Option<Frame>> {
        self.release();
        let err = unsafe {
            let f: unsafe extern "C" fn(u64, *mut NativeDevice, *mut OverlayView, u32) -> i32 = slot(self.view_if, SLOT_ACQUIRE_VIEW);
            f(self.handle, &mut *self.native, &mut *self.view, std::mem::size_of::<OverlayView>() as u32)
        };
        if err != 0 {
            bail!("SteamVR wouldn't share the headset view (overlay error {err})");
        }
        self.acquired = true;
        if self.view.texture.handle.is_null() {
            return Ok(None);
        }
        let data = unsafe { &*(self.view.texture.handle as *const VulkanTextureData) };
        let b = &self.view.bounds;
        let (tw, th) = (data.width as f32, data.height as f32);
        let origin = ((b.u_min.min(b.u_max) * tw).round() as i32, (b.v_min.min(b.v_max) * th).round() as i32);
        let width = ((b.u_max - b.u_min).abs() * tw).round() as u32;
        let height = ((b.v_max - b.v_min).abs() * th).round() as u32;
        if width == 0 || height == 0 {
            return Ok(None);
        }
        Ok(Some(Frame { image: vk::Image::from_raw(data.image), origin, width, height }))
    }
}

impl HeadsetView {
    /// Hands the last acquired frame back to SteamVR. Safe to call twice.
    pub fn release(&mut self) {
        if self.acquired {
            unsafe {
                let f: unsafe extern "C" fn(*mut OverlayView) -> i32 = slot(self.view_if, SLOT_RELEASE_VIEW);
                f(&mut *self.view);
            }
            self.acquired = false;
        }
    }
}

impl Drop for HeadsetView {
    fn drop(&mut self) {
        self.release();
    }
}
