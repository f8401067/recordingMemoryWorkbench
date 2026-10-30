/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bg: 'rgb(var(--bg-rgb) / <alpha-value>)',
        panel: 'rgb(var(--panel-rgb) / <alpha-value>)',
        panel2: 'rgb(var(--panel2-rgb) / <alpha-value>)',
        ink: 'rgb(var(--ink-rgb) / <alpha-value>)',
        muted: 'rgb(var(--muted-rgb) / <alpha-value>)',
        line: 'rgb(var(--line-rgb) / <alpha-value>)',
        brand: 'rgb(var(--brand-rgb) / <alpha-value>)',
        brand2: 'rgb(var(--brand2-rgb) / <alpha-value>)',
        soft: 'rgb(var(--soft-rgb) / <alpha-value>)',
      },
      borderRadius: {
        '2.5xl': '1.25rem',
      },
      boxShadow: {
        soft: '0 1px 2px rgba(16,18,28,.04), 0 8px 24px -12px rgba(16,18,28,.12)',
        lift: '0 12px 40px -16px rgba(16,18,28,.28)',
      },
      fontFamily: {
        sans: [
          '-apple-system', 'BlinkMacSystemFont', '"PingFang SC"', '"Hiragino Sans GB"',
          '"Microsoft YaHei"', '"Segoe UI"', 'Roboto', 'sans-serif',
        ],
      },
      keyframes: {
        'fade-up': { '0%': { opacity: '0', transform: 'translateY(6px)' }, '100%': { opacity: '1', transform: 'translateY(0)' } },
        pulseRing: {
          '0%': { transform: 'scale(.9)', opacity: '.7' },
          '70%': { transform: 'scale(1.6)', opacity: '0' },
          '100%': { transform: 'scale(1.6)', opacity: '0' },
        },
      },
      animation: {
        'fade-up': 'fade-up .28s ease-out both',
        'pulse-ring': 'pulseRing 2s cubic-bezier(.4,0,.6,1) infinite',
      },
    },
  },
  plugins: [],
};
