const root = __dirname.replace(/\\/g, '/');

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    `${root}/templates/**/*.html`,
    `${root}/**/templates/**/*.html`,
    `${root}/**/*.py`,
    `${root}/static/js/**/*.js`,
  ],
  theme: {
    extend: {
      colors: {
        // Double Bite unified warm gastronomic palette
        'db-ink': '#141312',
        'db-noir': '#24211E',
        'db-paper': '#FAF8F5',
        'db-boucle': '#F3EFE8',
        'db-surface': '#FFFFFF',
        'db-linen': '#E6E0D6',
        'db-text': '#1F1C19',
        'db-muted': '#57524C',
        'db-subtle': '#6E6861',
        'db-crust': '#C8743A',
        'db-crust-dark': '#A65622',
        'db-crust-soft': '#E9A66F',
        'db-success': '#166534',
        'db-warning': '#92400E',
        'db-danger': '#B91C1C',

        // Legacy fv-* aliases remapped to warm tokens during migration
        'fv-noir': '#24211E',
        'fv-offwhite': '#FAF8F5',
        'fv-slate': '#57524C',
        'fv-lightgray': '#E6E0D6',
        'fv-white': '#FFFFFF',
        'fv-boucle': '#F3EFE8',
        'fv-linen': '#E6E0D6',
        'fv-oak': '#D5C2AB',
        'fv-stone': '#DDD7CF',
      },
      fontFamily: {
        sans: ['"Manrope"', '-apple-system', 'BlinkMacSystemFont', 'sans-serif'],
        serif: ['"Cormorant Garamond"', 'Georgia', 'serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
    },
  },
  plugins: [],
}
