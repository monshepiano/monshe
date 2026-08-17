const s = (p: any) => ({
  width: p.size || 18, height: p.size || 18, viewBox: '0 0 24 24', fill: 'none',
  stroke: 'currentColor', strokeWidth: 1.7, strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
})

export const IcChat = (p: any) => <svg {...s(p)}><path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-4-.9L3 21l1.9-5a8.4 8.4 0 0 1-.9-4 8.4 8.4 0 0 1 8.5-8.4 8.4 8.4 0 0 1 8.5 8.4z"/></svg>
export const IcTask = (p: any) => <svg {...s(p)}><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
export const IcTerm = (p: any) => <svg {...s(p)}><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M6 9l3 3-3 3M13 15h5"/></svg>
export const IcBell = (p: any) => <svg {...s(p)}><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>
export const IcGear = (p: any) => <svg {...s(p)}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 9 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 9a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/></svg>
export const IcMic = (p: any) => <svg {...s(p)}><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M19 10a7 7 0 0 1-14 0M12 19v3"/></svg>
export const IcCam = (p: any) => <svg {...s(p)}><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>
export const IcClip = (p: any) => <svg {...s(p)}><path d="M21.4 11.05l-9.2 9.2a5 5 0 0 1-7.1-7.1l9.2-9.2a3.3 3.3 0 1 1 4.7 4.7l-9.2 9.2a1.7 1.7 0 0 1-2.4-2.4l8.5-8.5"/></svg>
export const IcSend = (p: any) => <svg {...s(p)}><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>
export const IcFile = (p: any) => <svg {...s(p)}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
export const IcBrain = (p: any) => <svg {...s(p)}><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24A2.5 2.5 0 0 1 9.5 2z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24A2.5 2.5 0 0 0 14.5 2z"/></svg>
export const IcX = (p: any) => <svg {...s(p)}><path d="M18 6L6 18M6 6l12 12"/></svg>
export const IcPlus = (p: any) => <svg {...s(p)}><path d="M12 5v14M5 12h14"/></svg>
export const IcPlay = (p: any) => <svg {...s(p)}><path d="M5 3l14 9-14 9V3z"/></svg>
export const IcTrash = (p: any) => <svg {...s(p)}><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
export const IcShield = (p: any) => <svg {...s(p)}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
export const IcTg = (p: any) => <svg {...s(p)}><path d="M22 3L2 10.5l6 2.3L20 6l-9 8.5.5 6L15 17l5 4z"/></svg>
export const IcMenu = (p: any) => <svg {...s(p)}><path d="M4 6h16M4 12h16M4 18h16"/></svg>
