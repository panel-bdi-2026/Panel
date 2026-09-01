/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          50:  '#eff6ff',
          100: '#dbeafe',
          200: '#bfdbfe',
          300: '#93c5fd',
          400: '#60a5fa',
          500: '#3b82f6',
          600: '#2563eb',
          700: '#1d4ed8',
          800: '#1e40af',
          900: '#1e3a8a',
          950: '#172554',
        },
        // Superficies con elevación (fondo → card → elevada → borde/overlay)
        surface: {
          0: '#0a0b0d',
          1: '#121316',
          2: '#1a1c20',
          3: '#232629',
        },
        // Color semántico de trading
        profit: { DEFAULT: '#22c55e', dim: '#16a34a' },
        loss:   { DEFAULT: '#ef4444', dim: '#dc2626' },
        warn:   { DEFAULT: '#f59e0b' },
      },
      boxShadow: {
        card: '0 1px 3px rgba(0,0,0,0.4)',
        elevated: '0 8px 30px rgba(0,0,0,0.5)',
      },
      borderRadius: { xl2: '1rem' },
    },
  },
  plugins: [],
}
