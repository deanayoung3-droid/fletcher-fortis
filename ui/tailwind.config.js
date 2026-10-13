module.exports = {
  content: ["../optout/templates/**/*.html", "../optout/charts.py"], darkMode: "class",
  theme: { extend: {
    fontFamily: { sans: ["Geist", "Inter", "ui-sans-serif", "system-ui", "sans-serif"], mono: ["Geist Mono", "JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"] },
    colors: { ink: "#0f1222", shell: "#f4f3ef", canvas: "#e7e5e0",
      accent: { 50:"#eef1ff",100:"#e0e5ff",200:"#c7cfff",300:"#a5b0fc",400:"#8592f7",500:"#6a76f0",600:"#5559e3",700:"#4646c8",800:"#3a3ba1",900:"#2f327d" } } } },
  plugins: [] };
