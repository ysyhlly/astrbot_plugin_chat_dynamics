// All sections share the host's single app scope. Keep its signed URL, shared UMO and theme.
const target = new URL("./console/index.html", window.location.href);
target.search = window.location.search;
try {
  if (!target.searchParams.get("umo")) {
    const sesUmo = (sessionStorage.getItem("dynamics_shared_umo") || "").trim();
    if (sesUmo) target.searchParams.set("umo", sesUmo);
  }
} catch {}
target.hash = window.location.hash;
window.location.replace(target.href);
