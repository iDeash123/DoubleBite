/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './templates/**/*.html',
    './**/templates/**/*.html',
    './**/*.py',
    './static/js/**/*.js',
  ],
  theme: {
    extend: {
      colors: {
        'fv-noir': '#262626',
        'fv-slate': '#8C8C8C',
        'fv-lightgray': '#E5E5E5',
        'fv-offwhite': '#F6F6F6',
        'fv-white': '#FFFFFF',
        'fv-boucle': '#F4F1EB',
        'fv-linen': '#E8E3DA',
        'fv-oak': '#D5C2AB',
        'fv-stone': '#DDD7CF',
      },
      fontFamily: {
        sans: ['"Plus Jakarta Sans"', '-apple-system', 'BlinkMacSystemFont', 'sans-serif'],
        serif: ['"Cormorant Garamond"', 'Georgia', 'serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
    },
  },
  plugins: [],
}
