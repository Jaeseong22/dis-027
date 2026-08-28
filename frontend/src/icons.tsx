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
