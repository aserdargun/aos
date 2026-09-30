fn allowed_navigation(url: &tauri::Url) -> bool {
    url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port() == Some(8765)
        && url.username().is_empty()
        && url.password().is_none()
        && url.path().starts_with("/ui/")
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::External("http://127.0.0.1:8765/ui/".parse()?),
            )
            .title("AOS · Kontrol merkezi")
            .inner_size(1440.0, 980.0)
            .min_inner_size(800.0, 600.0)
            .incognito(true)
            .devtools(false)
            .disable_drag_drop_handler()
            .on_navigation(allowed_navigation)
            .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
            .on_download(|_, _| false)
            .on_page_load(|_, payload| {
                if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) {
                    println!("AOS native UI page loaded");
                }
            })
            .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("AOS console could not start");
}

#[cfg(test)]
mod tests {
    use super::allowed_navigation;

    #[test]
    fn navigation_is_restricted_to_the_pinned_loopback_ui() {
        assert!(allowed_navigation(
            &"http://127.0.0.1:8765/ui/".parse().unwrap()
        ));
        for address in [
            "https://example.com/ui/",
            "http://127.0.0.1:8766/ui/",
            "http://localhost:8765/ui/",
            "http://127.0.0.1:8765/api/state",
            "http://user@127.0.0.1:8765/ui/",
            "file:///etc/passwd",
        ] {
            assert!(!allowed_navigation(&address.parse().unwrap()));
        }
    }
}
