document.getElementById("theme").addEventListener("click", function () {
  var d = document.documentElement.classList.toggle("dark");
  try { localStorage.setItem("ff-theme", d ? "dark" : "light"); } catch (e) {}
});
document.querySelectorAll("[data-copy]").forEach(function (b) {
  b.addEventListener("click", function () {
    navigator.clipboard && navigator.clipboard.writeText(b.dataset.copy).then(function () {
      b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy"; }, 1500);
    });
  });
});
