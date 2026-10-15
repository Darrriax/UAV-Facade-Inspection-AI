<script setup lang="ts">
/**
 * The reconstructed mesh, rendered with TresJS (Three.js) over a signed URL.
 *
 * Three details are doing real work here:
 *
 * * **The GLB is Draco-compressed.** ODM's `--gltf` export applies
 *   `KHR_draco_mesh_compression`, and `GLTFLoader` rejects such a file outright
 *   unless a `DRACOLoader` is attached - which is how a finished reconstruction
 *   ends up rendering as nothing at all. The decoder is served from
 *   `public/draco` (see `scripts/vendor-draco-decoder.mjs`) rather than from a
 *   CDN, so the viewer works on an offline workstation too.
 * * **Fit to camera.** ODM output is georeferenced, so the mesh sits far from
 *   the origin and at an arbitrary scale (UTM eastings are hundreds of
 *   thousands of metres). The camera is therefore positioned from the model's
 *   own bounding box - centre plus a distance derived from its radius and the
 *   field of view - rather than from a fixed position that would show empty
 *   space or the inside of the building.
 * * **Failure is a state, not an exception.** A `.glb` fetch can fail (expired
 *   URL, storage hiccup). `useGLTF`'s `isLoading` never flips to a value that
 *   distinguishes "slow" from "failed", so a bounded timer turns a load that is
 *   still pending after 20 s into a visible message instead of an endless
 *   spinner.
 *
 * ## Free camera, not a turntable
 *
 * The camera is three's `TrackballControls` (see `FreeCamera` below), not an
 * orbit rig. Orbit controls turn the view around the world's vertical axis and
 * clamp it at the poles: looking at a building from the side is easy, looking
 * at it from below - or from an angle you have already tilted past - is not,
 * because the drag keeps coming back to the same horizontal spin. A trackball
 * drags the subject itself in the plane of the screen, so a drag in any
 * direction turns the model through that direction and none of them is
 * privileged. Left-drag turns it (rolling the view when the drag does),
 * shift + left drag or a right drag moves it, a second finger does the same on
 * a touch screen, and the wheel dollies in and out.
 * None of that is discoverable from this page, which is why there is no hint
 * strip either: the gestures are the ones a 3D viewer is expected to have, so
 * documenting them would cost pixels over the building and buy nothing.
 *
 * ## The mesh and nothing else
 *
 * There is no toolbar over the canvas: no hint strip, no camera button, no
 * wireframe toggle. The page that shows a reconstruction is about the
 * reconstruction, and an overlay that covers the lower corner of the building
 * costs more than the shortcuts it documents. Loading and failure are the only
 * two things this component draws on top of the scene.
 *
 * The parent keys this component by model id: a different model mounts a fresh
 * viewer instead of re-pointing a live loader, which is why the URL is read
 * once and never watched.
 */

import { computed, defineComponent, onBeforeUnmount, ref, watch } from 'vue'
import type { PropType } from 'vue'
import { useI18n } from 'vue-i18n'

import { TresCanvas, useLoop, useTres } from '@tresjs/core'
import { useGLTF } from '@tresjs/cientos'
import { Box3, MOUSE, Vector3 } from 'three'
import type { Camera } from 'three'
import { TrackballControls } from 'three/examples/jsm/controls/TrackballControls.js'

import AppIcon from '@/components/AppIcon.vue'
import AppSpinner from '@/components/AppSpinner.vue'

const { t } = useI18n()

const props = defineProps<{
  /** Signed URL of the `.glb`; expires, so it is used now and never stored. */
  url: string
}>()

//: Files vendor'd into the build by `npm run vendor:draco` - DRACOLoader
//: fetches them by URL, so they cannot simply be imported.
const DRACO_DECODER_PATH = '/draco/'

/**
 * The camera: a trackball, mounted inside `<TresCanvas>` so it can reach the
 * camera and the canvas that TresJS created.
 *
 * The gestures are the ones the page's own header describes; what follows is
 * the wiring TresJS cannot do for a control it does not ship.
 *
 * `update()` has to run every frame: the damping, the pan and the dolly are all
 * integrated there, not in the event handlers - an instance that is never
 * updated accepts drags and shows nothing. `handleResize()` has to follow the
 * canvas, because a drag is mapped to a rotation through the element's own
 * size: a resize the control does not hear about leaves the drag pointing at
 * the wrong part of the screen. And the limits are expressed against the
 * model's own radius, since ODM's units are metres in an arbitrary projection.
 */
const FreeCamera = defineComponent({
  name: 'FreeCamera',
  props: {
    /** Where the model is framed from: the parent's fit, in world units. */
    position: { type: Array as unknown as PropType<[number, number, number]>, required: true },
    /** What the camera turns around and pans relative to: the model's centre. */
    target: { type: Array as unknown as PropType<[number, number, number]>, required: true },
    /** Half the model's largest extent, for the zoom limits below. */
    radius: { type: Number, required: true },
  },
  setup(props) {
    const { camera, renderer } = useTres()
    const element = renderer.domElement

    let trackball: TrackballControls | null = null

    //: The element's size is what a drag is measured against, so a canvas that
    //: changes size has to tell the control about it.
    const resized = new ResizeObserver(() => trackball?.handleResize())

    /**
     * Limits, in model units: enough to keep the building in reach without
     * fencing the camera in. Only the *distance* is bounded - the camera can
     * still turn to any direction, including under the model.
     */
    function applyLimits(): void {
      if (!trackball) return
      trackball.minDistance = props.radius * 0.05
      trackball.maxDistance = props.radius * 30
    }

    function applyTarget(): void {
      trackball?.target.set(props.target[0], props.target[1], props.target[2])
    }

    /**
     * Put the camera where the parent's fit wants it.
     *
     * The camera's position is applied here rather than left to TresJS's own
     * `position` binding, which lands a tick later: the controls aim the camera
     * at the target inside `update()`, so a position that arrives *after* the
     * last update leaves the view pointing where the camera used to be - the
     * model framed off-centre, until something (a drag, a frame of the loop)
     * happens to correct it. One function, one moment, no ordering to get wrong.
     */
    function placeCamera(): void {
      if (!trackball) return
      trackball.object.position.set(props.position[0], props.position[1], props.position[2])
      applyTarget()
      applyLimits()
      trackball.update()
    }

    /**
     * Build the control as soon as TresJS has a camera to hand it.
     *
     * The perspective camera is declared beside this component, and a camera
     * registers itself with the manager when it mounts - which is after the
     * first render, so on the first pass there is nothing to control yet. That
     * is also why this is a watcher rather than a line in `setup`: the control
     * is created on the first frame that has a camera, and never twice.
     */
    watch(
      () => camera.value,
      (value) => {
        if (!value || trackball) return
        trackball = new TrackballControls(value as Camera, element)

        //: A third of the canvas is a comfortable quarter-turn; three's 1:1
        //: default reads as sluggish on a building-sized subject that is always
        //: seen from far away.
        trackball.rotateSpeed = 1.5
        //: Panning is scaled by the distance to the target, so this is a share
        //: of the viewport the scene follows, not a distance - the same drag
        //: moves the model the same way whether the camera is close or far. 0.8
        //: is close to 1:1 for this camera: the scene follows the pointer
        //: instead of lagging behind it.
        trackball.panSpeed = 0.8
        trackball.zoomSpeed = 1.2
        //: Inertia, deliberately short. A trackball that keeps spinning after
        //: the pointer stops reads as a camera with a mind of its own - the view
        //: has to stay where it was put, and this page has nowhere to explain a
        //: long glide away from it. 0.65 leaves a flick of momentum (a fast drag
        //: coasts a few degrees) and nothing more.
        trackball.staticMoving = false
        trackball.dynamicDampingFactor = 0.65

        placeCamera()
        resized.observe(element)
      },
      { immediate: true },
    )

    const { onBeforeRender } = useLoop()
    //: `update()` is where the drag's momentum, the pan and the dolly are
    //: applied: an instance that is never updated accepts input and renders
    //: none of it.
    onBeforeRender(() => trackball?.update())

    /**
     * Shift turns a left drag into a pan.
     *
     * The map the control ships puts panning on the right button - how a
     * three-button mouse expects it, and precisely what a trackpad cannot press
     * comfortably. Shift + drag is the gesture people reach for with such a
     * viewer, so it leads to the same state. The action is read once, at
     * pointerdown, and kept for the whole drag, so the map only has to be right
     * at that moment; the listener sits on the window in the capture phase
     * because the control's own listener is on the canvas, and for an event
     * dispatched *at* that canvas registration order decides, not the capture
     * flag.
     */
    function onPointerDownCapture(event: PointerEvent): void {
      if (!trackball || event.target !== element || event.button !== 0) return
      trackball.mouseButtons.LEFT = event.shiftKey ? MOUSE.PAN : MOUSE.ROTATE
    }
    window.addEventListener('pointerdown', onPointerDownCapture, true)

    watch([() => props.position, () => props.target, () => props.radius], placeCamera)

    onBeforeUnmount(() => {
      window.removeEventListener('pointerdown', onPointerDownCapture, true)
      resized.disconnect()
      // Detaches the pointer and wheel listeners the constructor attached, so a
      // viewer that is gone cannot be driven by events meant for the next one.
      trackball?.dispose()
      trackball = null
    })

    // Nothing to render: this component *is* the control, and the canvas around
    // it is TresJS's own element.
    return () => null
  },
})

const { state, isLoading } = useGLTF(props.url, {
  draco: true,
  decoderPath: DRACO_DECODER_PATH,
})

const cameraPosition = ref<[number, number, number]>([3, 2, 5])
const controlsTarget = ref<[number, number, number]>([0, 0, 0])
//: Half the model's largest extent, so the camera limits in `FreeCamera` can be
//: expressed relative to the building rather than in absolute units - ODM's are
//: metres in an arbitrary projection, not pixels.
const modelRadius = ref(1)
const timedOut = ref(false)
const rendererFailed = ref(false)

//: Bounded wait before "still loading" becomes "could not load".
const timeout = window.setTimeout(() => {
  timedOut.value = true
}, 20_000)
onBeforeUnmount(() => window.clearTimeout(timeout))

const failed = computed(() => rendererFailed.value || (timedOut.value && !state.value))
const loading = computed(() => !state.value && !failed.value)

//: Vertical field of view in degrees; reused by the fit maths below.
const FOV = 45

/**
 * Frame the whole model, once its scene is parsed.
 *
 * Nothing else positions the camera: ODM's mesh is georeferenced, so a fixed
 * position would show either empty space or the inside of the building.
 */
function fitCamera(): void {
  const scene = state.value?.scene
  if (!scene) return

  const box = new Box3().setFromObject(scene)
  if (box.isEmpty()) return

  const size = box.getSize(new Vector3())
  const centre = box.getCenter(new Vector3())
  //: Half the largest extent, floored so a degenerate mesh still frames.
  const radius = Math.max(size.x, size.y, size.z) / 2 || 1
  const distance = (radius / Math.tan((FOV * Math.PI) / 360)) * 1.4

  cameraPosition.value = [
    centre.x + distance * 0.55,
    centre.y + distance * 0.4,
    centre.z + distance * 0.75,
  ]
  controlsTarget.value = [centre.x, centre.y, centre.z]
  modelRadius.value = radius
}

watch(state, (loaded) => {
  if (!loaded) return
  fitCamera()
})
</script>

<template>
  <div class="relative h-full w-full overflow-hidden bg-slate-950">
    <TresCanvas
      class="h-full w-full"
      clear-color="#0a1018"
      :antialias="true"
      :window-size="false"
      @error="rendererFailed = true"
    >
      <TresPerspectiveCamera :position="cameraPosition" :fov="FOV" :near="0.01" :far="500000" />
      <FreeCamera
        :position="cameraPosition"
        :target="controlsTarget"
        :radius="modelRadius"
      />

      <!--
        Ambient fill plus two opposing directional lights: a single light would
        leave the facade's far side black, and a photogrammetry mesh is often
        lit from one direction in its own texture.
      -->
      <TresAmbientLight :intensity="1.6" />
      <TresDirectionalLight :position="[3, 5, 4]" :intensity="2.2" />
      <TresDirectionalLight :position="[-4, -2, -3]" :intensity="0.9" />

      <primitive v-if="state?.scene" :object="state.scene" />
    </TresCanvas>

    <!-- Loading -->
    <div
      v-if="loading"
      class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-slate-950/90 text-slate-300"
    >
      <AppSpinner label="" class="scale-125" />
      <p class="text-sm">
        {{
          isLoading
            ? t('inspector.model3d.viewer.downloading')
            : t('inspector.model3d.viewer.preparing')
        }}
      </p>
    </div>

    <!-- Failure: the 2D inspector still has everything. -->
    <div
      v-else-if="failed"
      class="absolute inset-0 flex flex-col items-center justify-center gap-2 px-8 text-center"
    >
      <AppIcon name="alert" class="h-6 w-6 text-amber-400" />
      <p class="text-sm font-medium text-slate-200">
        {{ t('inspector.model3d.viewer.failedTitle') }}
      </p>
      <p class="max-w-sm text-xs text-slate-400">
        {{ t('inspector.model3d.viewer.failedBody') }}
      </p>
    </div>

  </div>
</template>
