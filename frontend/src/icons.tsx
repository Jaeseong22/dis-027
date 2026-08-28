/** 인라인 SVG 아이콘 — 아이콘 라이브러리를 넣을 만큼 쓰지 않는다. */
const base = {
  width: 18,
  height: 18,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.8,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
}

export const PlusIcon = () => (
  <svg {...base}><path d="M12 5v14M5 12h14" /></svg>
)

export const MenuIcon = () => (
  <svg {...base}><path d="M4 7h16M4 12h16M4 17h16" /></svg>
)

export const SendIcon = () => (
  <svg {...base} width={20} height={20}><path d="M12 19V5M5 12l7-7 7 7" /></svg>
)

export const ChatIcon = () => (
  <svg {...base} width={15} height={15}>
    <path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-4.5A8 8 0 0 1 11 4h2a8 8 0 0 1 8 8Z" />
  </svg>
)

export const DocIcon = () => (
  <svg {...base} width={15} height={15}>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" />
    <path d="M14 3v5h5M9 13h6M9 17h4" />
  </svg>
)

export const FlowIcon = () => (
  <svg {...base} width={15} height={15}>
    <path d="M6 4v5a3 3 0 0 0 3 3h9M6 12v3a3 3 0 0 0 3 3h9" />
    <path d="M15 9l3-3-3-3M15 21l3-3-3-3" />
  </svg>
)

export const CodeIcon = () => (
  <svg {...base} width={15} height={15}><path d="M9 18l-6-6 6-6M15 6l6 6-6 6" /></svg>
)

export const CopyIcon = () => (
  <svg {...base} width={14} height={14}>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1" />
  </svg>
)

export const CheckIcon = () => (
  <svg {...base} width={14} height={14}><path d="M20 6L9 17l-5-5" /></svg>
)
