import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { viteStaticCopy } from 'vite-plugin-static-copy'

export default defineConfig({
  plugins: [
    react(),
    viteStaticCopy({ targets: [
      { src: 'node_modules/cesium/Build/Cesium/Workers', dest: 'cesium' },
      { src: 'node_modules/cesium/Build/Cesium/Assets', dest: 'cesium' },
      { src: 'node_modules/cesium/Build/Cesium/Widgets', dest: 'cesium' },
      { src: 'node_modules/cesium/Build/Cesium/ThirdParty', dest: 'cesium' },
    ] }),
  ],
  define: { CESIUM_BASE_URL: JSON.stringify('/cesium') },
  server: {
    host: '0.0.0.0', port: 5173, strictPort: true,
    proxy: { '/v1': 'http://127.0.0.1:8000', '/assets': 'http://127.0.0.1:8000' },
  },
  preview: {
    host: '0.0.0.0', port: 5173, strictPort: true,
    proxy: { '/v1': 'http://127.0.0.1:8000', '/assets': 'http://127.0.0.1:8000' },
  },
  build: { target: 'es2022', assetsDir: 'bundled' },
})
