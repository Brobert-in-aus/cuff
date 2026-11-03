use std::path::PathBuf;
use std::process::Command;

fn main() {
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap());
    for name in ["convert", "stabilise"] {
        let src = format!("shaders/{name}.comp");
        println!("cargo::rerun-if-changed={src}");
        let status = Command::new("glslc")
            .args(["-O", "--target-env=vulkan1.1", "-o"])
            .arg(out.join(format!("{name}.spv")))
            .arg(&src)
            .status()
            .expect("glslc not found, install shaderc");
        assert!(status.success(), "failed to compile {src}");
    }
}
