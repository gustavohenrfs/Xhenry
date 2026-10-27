// Tailwind compilado (antes vinha do CDN e montava o CSS no navegador a cada abertura).
// Depois de usar uma classe nova em templates/index.html ou static/js/app.js, gere de novo:
//   sh scripts/build_css.sh
module.exports = {
  content: ["./templates/index.html", "./static/js/app.js"],
  theme: {
    extend: {
      fontFamily: { display: ["Space Grotesk", "Syne", "sans-serif"], sans: ["Outfit", "Space Grotesk", "sans-serif"] },
      colors: {
        ink: "#07050a",
        panel: "#100914",
        card: "#140b18",
        neon: "#ff2ec8",
        blush: "#ffd6f2",
        mute: "#b39ab4"
      }
    }
  }
};
