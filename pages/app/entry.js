// All sections share the host's single app scope. Keep its signed URL and theme.
const target = new URL("./console/index.html", window.location.href);
target.search = window.location.search;
target.hash = window.location.hash;
window.location.replace(target.href);
