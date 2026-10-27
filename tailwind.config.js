// Tailwind compilado (antes vinha do CDN e montava o CSS no navegador a cada abertura).
// Depois de usar uma classe nova em templates/index.html ou static/js/app.js, gere de novo:
//   sh scripts/build_css.sh
// As cores da marca vêm de variáveis CSS (canais "r g b" em static/css/styles.css), então as mesmas
// classes valem no tema escuro e no claro; o "/20" de opacidade continua funcionando.
const channel = (name) => `rgb(var(--c-${name}) / <alpha-value>)`;

module.exports = {
  content: ["./templates/index.html", "./static/js/app.js"],
  theme: {
    extend: {
      fontFamily: { display: ["Space Grotesk", "sans-serif"], sans: ["Outfit", "Space Grotesk", "sans-serif"] },
      colors: {
        ink: channel("ink"),
        panel: channel("panel"),
        card: channel("card"),
        neon: channel("neon"),
        blush: channel("blush"),
        mute: channel("mute")
      }
    }
  }
};
