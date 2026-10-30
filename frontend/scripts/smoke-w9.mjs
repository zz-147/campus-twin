#!/usr/bin/env node
/**
 * W9 冒烟模式快捷入口：等价于
 *   $env:SMOKE_MODE='w9'; $env:SMOKE_URL='http://127.0.0.1:5176/'; node scripts/smoke-test.mjs
 *
 * 前置：
 *   1) node scripts/mock-w9-server.mjs                     （backend 8002 + agent 8102）
 *   2) 另开终端启动 dev server 5176：
 *      $env:VITE_API_BASE='http://127.0.0.1:8002'
 *      $env:VITE_AGENT_BASE='http://127.0.0.1:8102'
 *      npm run dev -- --port 5176
 */
process.env.SMOKE_MODE = process.env.SMOKE_MODE || 'w9';
process.env.SMOKE_URL = process.env.SMOKE_URL || 'http://127.0.0.1:5176/';
await import('./smoke-test.mjs');
