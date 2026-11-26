import path from 'path';
import { fileURLToPath } from 'url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const configRoot = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig(() => {
    return {
      cacheDir: process.env.OPTPILOT_INTERFACE_VITE_CACHE_DIR || path.resolve(configRoot, 'node_modules/.vite'),
      server: {
        port: 3000,
        host: '0.0.0.0',
      },
      plugins: [react()],
      resolve: {
        alias: {
          '@': path.resolve(configRoot, '.'),
        }
      }
    };
});
