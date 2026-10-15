/**
 * Icon path data.
 *
 * Hand-rolled 24x24 stroke icons on purpose: the app needs about a dozen of
 * them, and an icon package would add a dependency, a bundle and a second
 * styling language (fill vs stroke, sizing props) for no benefit at this size.
 * Every icon is a list of SVG path `d` commands drawn with `stroke-width: 1.75`
 * and round caps - see `AppIcon.vue`.
 *
 * Names describe meaning, not shape (`mapPin`, not `markerOutline`), so the
 * paths can be redrawn without touching call sites.
 */

export const ICONS = {
  plus: ['M12 5v14', 'M5 12h14'],
  menu: ['M4 7h16', 'M4 12h16', 'M4 17h16'],
  close: ['M6 6l12 12', 'M18 6L6 18'],
  grid: ['M4 4h6.5v6.5H4z', 'M13.5 4H20v6.5h-6.5z', 'M4 13.5h6.5V20H4z', 'M13.5 13.5H20V20h-6.5z'],
  mapPin: [
    'M12 21.5s-6.5-5.75-6.5-11a6.5 6.5 0 1 1 13 0c0 5.25-6.5 11-6.5 11z',
    'M12 7.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z',
  ],
  cube: [
    'M12 2.75l8 4.5v9l-8 4.5-8-4.5v-9l8-4.5z',
    'M4 7.25l8 4.5 8-4.5',
    'M12 11.75v9',
  ],
  image: [
    'M3.5 4.5h17v15h-17z',
    'M9.25 10.5a1.75 1.75 0 1 0 0-3.5 1.75 1.75 0 0 0 0 3.5z',
    'M4 17l5-5 4 4 3-2.5 4 4',
  ],
  alert: ['M12 3.5l9 17H3l9-17z', 'M12 10v4.5', 'M12 17.5v.01'],
  info: ['M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17z', 'M12 11v5.5', 'M12 7.75v.01'],
  upload: [
    'M12 16V4.5',
    'M7.5 8.75L12 4.25l4.5 4.5',
    'M4.5 15v3A2.5 2.5 0 0 0 7 20.5h10a2.5 2.5 0 0 0 2.5-2.5v-3',
  ],
  folder: [
    'M3.5 6.5A1.5 1.5 0 0 1 5 5h3.6a1.5 1.5 0 0 1 1.2.6l1 1.4H19a1.5 1.5 0 0 1 1.5 1.5v8.5A1.5 1.5 0 0 1 19 18.5H5a1.5 1.5 0 0 1-1.5-1.5z',
  ],
  // A pencil, not a gear: the action edits this project's own details.
  edit: [
    'M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z',
    'M14.5 5.5l4 4',
  ],
  trash: [
    'M4.5 7.25h15',
    'M9.5 4.5h5',
    'M6.5 7.25l.8 11.1a2 2 0 0 0 2 1.9h5.4a2 2 0 0 0 2-1.9l.8-11.1',
    'M10.5 11v5',
    'M13.5 11v5',
  ],
  refresh: [
    'M4.5 11.5a7.5 7.5 0 0 1 12.8-4.6l2.2 2.1',
    'M19.5 12.5a7.5 7.5 0 0 1-12.8 4.6l-2.2-2.1',
    'M19.5 4.2v4.8h-4.8',
    'M4.5 19.8v-4.8h4.8',
  ],
  clock: ['M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17z', 'M12 7.5V12l3.25 2'],
  // The annotated / plain-photo switch on the review canvas.
  eye: [
    'M2.75 12S6.5 5.75 12 5.75 21.25 12 21.25 12 17.5 18.25 12 18.25 2.75 12 2.75 12z',
    'M12 9.25a2.75 2.75 0 1 0 0 5.5 2.75 2.75 0 0 0 0-5.5z',
  ],
  eyeOff: [
    'M2.75 12S6.5 5.75 12 5.75 21.25 12 21.25 12 17.5 18.25 12 18.25 2.75 12 2.75 12z',
    'M12 9.25a2.75 2.75 0 1 0 0 5.5 2.75 2.75 0 0 0 0-5.5z',
    'M4 4l16 16',
  ],
  /**
   * The annotation tools: a brush and an eraser, the two halves of one gesture
   * (paint a mask, rub part of one out).
   */
  brush: [
    'M18 2.5l3.5 3.5-9.5 9.5-3.5-3.5z',
    'M8.5 12.5l-1.6 2.1-1.4 2.4-3 1 1-3 2.4-1.4 2.1-1.6z',
  ],
  eraser: [
    'M7.5 20.5h13.5',
    'M7 20.5l-3.8-3.8a2.5 2.5 0 0 1 0-3.5l9.2-9.2a2.5 2.5 0 0 1 3.5 0l5.6 5.6a2.5 2.5 0 0 1 0 3.5l-8.4 8.4z',
    'M5.5 11.5l8 8',
  ],
  arrowRight: ['M4.5 12h15', 'M13.5 6l6 6-6 6'],
  arrowLeft: ['M19.5 12h-15', 'M10.5 6l-6 6 6 6'],
  check: ['M5 12.5l4.5 4.5L19 7'],
  // Pipeline transport controls: the two bars read as "hold", the triangle as
  // "carry on", which is exactly what the two buttons on the progress screen do.
  pause: ['M9.5 5v14', 'M14.5 5v14'],
  play: ['M7.5 5l11 7-11 7z'],
  user: [
    'M12 4.5a3.75 3.75 0 1 0 0 7.5 3.75 3.75 0 0 0 0-7.5z',
    'M4.5 20.5a7.5 7.5 0 0 1 15 0',
  ],
  logOut: [
    'M15.5 4.5h3a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-3',
    'M10 16.5L5.5 12 10 7.5',
    'M5.5 12h9',
  ],
} as const

export type IconName = keyof typeof ICONS
