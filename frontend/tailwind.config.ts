import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Plus Jakarta Sans"', '"PingFang SC"', '"Hiragino Sans GB"', '"Microsoft YaHei"', '"SimHei"', '-apple-system', 'BlinkMacSystemFont', 'sans-serif'],
        mono: ['"Space Mono"', 'monospace']
      }
    }
  },
  plugins: []
} satisfies Config;
