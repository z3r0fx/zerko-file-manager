/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // The grey scale the whole UI is built from, driven by CSS variables so
        // Appearance can swap the background tone (pure black, midnight, warm...).
        zinc: {
          50: "rgb(var(--z-50) / <alpha-value>)",
          100: "rgb(var(--z-100) / <alpha-value>)",
          200: "rgb(var(--z-200) / <alpha-value>)",
          300: "rgb(var(--z-300) / <alpha-value>)",
          400: "rgb(var(--z-400) / <alpha-value>)",
          500: "rgb(var(--z-500) / <alpha-value>)",
          600: "rgb(var(--z-600) / <alpha-value>)",
          700: "rgb(var(--z-700) / <alpha-value>)",
          800: "rgb(var(--z-800) / <alpha-value>)",
          900: "rgb(var(--z-900) / <alpha-value>)",
          950: "rgb(var(--z-950) / <alpha-value>)",
        },
        // The brand accent, driven by a CSS variable so it can be changed at
        // runtime. Written as rgb(... / <alpha-value>) rather than a raw
        // var(), which is what lets bg-accent/15 and ring-accent/60 work.
        accent: {
          DEFAULT: "rgb(var(--accent-rgb) / <alpha-value>)",
          hi: "rgb(var(--accent-hi-rgb) / <alpha-value>)",
          lo: "rgb(var(--accent-lo-rgb) / <alpha-value>)",
          // readable ink for text sitting ON an accent fill
          foreground: "rgb(var(--accent-ink) / <alpha-value>)",
        },
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
      },
      borderRadius: {
        lg: "var(--radius, 0.5rem)",
        md: "max(0px, calc(var(--radius, 0.5rem) - 2px))",
        sm: "max(0px, calc(var(--radius, 0.5rem) - 4px))",
      },
    },
  },
  plugins: [],
}
