import { defineConfig } from 'vitepress';
import { withMermaid } from 'vitepress-plugin-mermaid';

export default withMermaid(
  defineConfig({
  title: 'Hippo 文档中心',
  description: '🦛 Hippo 统一跨 IDE 记忆中枢与工程架构文档',
  base: '/hippo/',
  lang: 'zh-CN',

  themeConfig: {
    siteTitle: '🦛 Hippo',

    search: {
      provider: 'local',
      options: {
        translations: {
          button: {
            buttonText: '搜索文档',
            buttonAriaLabel: '搜索文档',
          },
          modal: {
            noResultsText: '无法找到相关结果',
            resetButtonTitle: '清除查询条件',
            footer: {
              selectText: '选择',
              navigateText: '切换',
              closeText: '关闭',
            },
          },
        },
      },
    },

    nav: [
      { text: '首页', link: '/' },
      { text: '系统架构', link: '/architecture/' },
      { text: '架构决策 (ADR)', link: '/adr/' },
      { text: '技术提案 (RFC)', link: '/rfcs/' },
      { text: '实施计划', link: '/plans/' },
      { text: '运维手册', link: '/runbooks/' },
      { text: '领域知识', link: '/knowledge/' },
      { text: 'Agent 协同', link: '/agents/' },
    ],

    sidebar: {
      '/architecture/': [
        {
          text: '系统架构',
          items: [
            { text: '架构总览与设计哲学', link: '/architecture/' },
            { text: '分层记忆治理数据流', link: '/architecture/data-flow' },
            { text: '宿主适配与契约矩阵', link: '/architecture/host-matrix' },
          ],
        },
      ],
      '/adr/': [
        {
          text: '架构决策记录 (ADR)',
          items: [
            { text: 'ADR 索引与总览', link: '/adr/' },
            { text: 'ADR 标准模板', link: '/adr/template' },
          ],
        },
        {
          text: '核心架构决策',
          collapsed: false,
          items: [
            { text: '0001: 轻量封装 Mem0 与最小依赖', link: '/adr/0001-thin-wrapper-over-mem0' },
            { text: '0002: 会话记忆异步蒸馏与双事件', link: '/adr/0002-session-memory-distillation' },
            { text: '0003: Cold Path 记忆合并与生命周期', link: '/adr/0003-cold-path-memory-consolidation' },
            { text: '0004: 向量 Profile 隔离与重建迁移', link: '/adr/0004-embedding-profile-isolation-and-migration' },
            { text: '0005: Spool 队列治理与 Worker 常驻保活', link: '/adr/0005-spool-governance-and-worker-service' },
          ],
        },
      ],
      '/rfcs/': [
        {
          text: '技术方案提案 (RFC)',
          items: [
            { text: 'RFC 提案索引', link: '/rfcs/' },
            { text: 'RFC 标准模板', link: '/rfcs/template' },
            { text: '0001: Jev 离线关系分类后端', link: '/rfcs/0001-jev-cold-path-classifier' },
          ],
        },
      ],
      '/plans/': [
        {
          text: '实施计划 (Plans)',
          items: [
            { text: '计划概览', link: '/plans/' },
            { text: '实施计划模板', link: '/plans/template' },
          ],
        },
      ],
      '/runbooks/': [
        {
          text: '运维与排障手册',
          items: [
            { text: '手册索引', link: '/runbooks/' },
            { text: 'Qdrant 本地常驻与部署运维', link: '/runbooks/deployment' },
            { text: 'Doctor 巡检与环境排障', link: '/runbooks/troubleshooting' },
            { text: '记忆迁移与向量重建操作手册', link: '/runbooks/migration' },
          ],
        },
      ],
      '/knowledge/': [
        {
          text: '领域知识库',
          items: [
            { text: '知识库概览', link: '/knowledge/' },
            { text: 'Jev 结构化决策后端调研', link: '/knowledge/jev-research' },
            { text: 'Codex 记忆机制深度调研', link: '/knowledge/codex-memory-research' },
            { text: '会话记忆蒸馏工程调研', link: '/knowledge/session-distill-research' },
            { text: 'Cold Path 记忆固化与索引设计', link: '/knowledge/cold-path-consolidation-design' },
            { text: 'Mem0 内部机制与边界剖析', link: '/knowledge/mem0-deep-dive' },
          ],
        },
      ],
      '/agents/': [
        {
          text: 'Agent 协同规范',
          items: [
            { text: 'Agent 协同契约与记忆检索', link: '/agents/' },
          ],
        },
      ],
    },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/mungerism/hippo' },
    ],


    outline: {
      level: [2, 3],
      label: '页面导航',
    },

    docFooter: {
      prev: '上一篇',
      next: '下一篇',
    },
  },
}));
