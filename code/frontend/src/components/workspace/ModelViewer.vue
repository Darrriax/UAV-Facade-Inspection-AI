<script setup lang="ts">
/**
 * The reconstructed mesh, rendered with TresJS (Three.js) over a signed URL.
 *
 * Four details are doing real work here:
 *
 * * **The GLB is Draco-compressed.** ODM's `--gltf` export applies
 *   `KHR_draco_mesh_compression`, and `GLTFLoader` rejects such a file outright
 *   unless a `DRACOLoader` is attached. The decoder is served from
 *   `public/draco` (see `scripts/vendor-draco-decoder.mjs`) rather than from a
 *   CDN, so the viewer works on an offline workstation too.
 * * **Z is up.** ODM's georeferenced mesh keeps its own frame: the ground is the
 *   XY plane and Z is altitude. three.js assumes Y is up, so the camera's up
 *   vector is set to Z, the orbit turns around that axis, and the initial fit
 *   uses the same convention. Turning the view therefore never tilts the
 *   building, and the orbit pivots on the centre of the whole model.
 * * **Fit to the model.** ODM output is georeferenced, so the mesh sits far from
 *   the origin and at an arbitrary scale. The camera is positioned from the
 *   model's own bounding box rather than from a fixed position.
 * * **Failure is a state, not a timer.** The loader reports a real load error.
 *   A slow download or a Draco decode is still loading, never a failure, so the
 *   viewer shows its failure state only when the loader says so, and asks the
 *   parent once to refresh the URL, since a signed URL can expire.
 *
 * ## Rendering on demand
 *
 * The canvas draws only when something changed: the camera moved, the canvas
 * was resized, the tab came back to the foreground, or the WebGL context was
 * restored. An idle viewer draws nothing, so a page left open - or in a
 * background tab that is still listed - does not keep the GPU busy and the
 * laptop warm.
 *
 * ## Camera
 *
 * `OrbitControls` around the model's centre: a left drag orbits around the
 * vertical axis, shift (or right) drag pans, the wheel zooms, and touch works
 * the same way. Damping is off, because damping needs frames after the pointer
 * stops and an on-demand canvas has none to spend.
 *
 * ## The mesh and nothing else
 *
 * There is no toolbar over the canvas. Loading and failure are the only two
 * things this component draws on top of the scene.
 *
 * The parent keys this component by model URL, so a different URL mounts a
 * fresh viewer instead of re-pointing a live loader; the URL is read once and
 * never watched.
 */

import { computed, defineComponent, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import type { PropType } from 'vue'
import { useI18n } from 'vue-i18n'

import { TresCanvas, useTres } from '@tresjs/core'
import { Box3, Vector3 } from 'three'
import type { Camera } from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { DRACOLoader } from 'three/examples/jsm/loaders/DRACOLoader.js'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import type { GLTF } from 'three/examples/jsm/loaders/GLTFLoader.js'

import AppIcon from '@/components/AppIcon.vue'
import AppSpinner from '@/components/AppSpinner.vue'

const { t } = useI18n()

const props = defineProps<{
  /** Signed URL of the `.glb`; expires, so it is used now and never stored. */
  url: string
}>()

const emit = defineEmits<{
  /** The loader could not fetch or parse the model. */
  failed: []
}>()

//: Files vendor'd into the build by `npm run vendor:draco` - DRACOLoader
//: fetches them by URL, so they cannot simply be imported.
const DRACO_DECODER_PATH = '/draco/'

//: ODM's georeferenced frame: Z is the altitude, so Z is what is "up".
const UP: [number, number, number] = [0, 0, 1]

/**
 * The camera: orbit controls, mounted inside `<TresCanvas>` so it can reach the
 * camera and the canvas that TresJS created.
 *
 * `update()` is what applies the orbit, the pan and the dolly, and every
 * applied change dispatches `change`; that event is what asks the on-demand
 * canvas for a frame. `handleResize` is not needed: the controls read the
 * element's size when a drag starts, and the observer below only asks for a
 * frame so the new size is drawn.
 */
const OrbitCamera = defineComponent({
  name: 'OrbitCamera',
  props: {
    /** Where the model is framed from: the parent's fit, in world units. */
    position: { type: Array as unknown as PropType<[number, number, number]>, required: true },
    /** What the camera orbits around and pans relative to: the model's centre. */
    target: { type: Array as unknown as PropType<[number, number, number]>, required: true },
    /** Half the model's largest extent, for the zoom limits below. */
    radius: { type: Number, required: true },
  },
  setup(props) {
    const { camera, renderer, invalidate } = useTres()
    const element = renderer.domElement

    let controls: OrbitControls | null = null

    const resized = new ResizeObserver(() => invalidate())

    /** Limits relative to the model, so they hold for any projection and scale. */
    function applyLimits(): void {
      if (!controls) return
      controls.minDistance = props.radius * 0.05
      controls.maxDistance = props.radius * 30
    }

    /**
     * Put the camera where the parent's fit wants it, and aim it at the centre.
     *
     * Done in one function at one moment: the controls derive the orbit from
     * the camera's position relative to the target, so the two have to be set
     * together, and `update()` then makes both take effect.
     */
    function placeCamera(): void {
      if (!controls) return
      controls.object.position.set(props.position[0], props.position[1], props.position[2])
      controls.target.set(props.target[0], props.target[1], props.target[2])
      applyLimits()
      controls.update()
    }

    /** A context restored by the browser has lost its GPU resources: draw again. */
    function onContextRestored(): void {
      invalidate()
    }

    /** True between a drag or wheel step's `start` and `end` events. */
    let interacting = false

    /** Applies pending input once per frame while the user is interacting. */
    function pumpOrbit(): void {
      if (!interacting || !controls) return
      controls.update()
      window.requestAnimationFrame(pumpOrbit)
    }

    function startOrbit(): void {
      if (interacting) return
      interacting = true
      pumpOrbit()
    }

    /** Last application of the input: a wheel step is applied only here. */
    function endOrbit(): void {
      interacting = false
      controls?.update()
    }

    /** Coming back to the tab: the canvas may be stale after being hidden. */
    function onVisibilityChange(): void {
      if (document.visibilityState === 'visible') invalidate()
    }

    /**
     * Build the controls as soon as TresJS has a camera to hand them.
     *
     * The perspective camera is declared beside this component and registers
     * itself after the first render, so on the first pass there is nothing to
     * control yet. The up vector has to be set before the controls read it.
     */
    watch(
      () => camera.value,
      (value) => {
        if (!value || controls) return
        value.up.set(UP[0], UP[1], UP[2])
        controls = new OrbitControls(value as Camera, element)

        //: Damping would need frames after the pointer stops; the canvas only
        //: draws on demand, so the orbit simply stops where the pointer does.
        controls.enableDamping = false
        controls.rotateSpeed = 1
        controls.panSpeed = 0.8
        controls.zoomSpeed = 1.2
        //: Every applied change asks for a frame - the only way the canvas
        //: learns that anything moved.
        controls.addEventListener('change', () => invalidate())
        //: OrbitControls only applies a drag or a wheel step inside `update()`, and
        //: the usual render loop that would call it every frame is gone. So the
        //: controls are pumped from `start` until `end`, and idle draws nothing.
        controls.addEventListener('start', startOrbit)
        controls.addEventListener('end', endOrbit)

        placeCamera()
        resized.observe(element)
      },
      { immediate: true },
    )

    watch([() => props.position, () => props.target, () => props.radius], placeCamera)

    element.addEventListener('webglcontextrestored', onContextRestored)
    document.addEventListener('visibilitychange', onVisibilityChange)

    onBeforeUnmount(() => {
      element.removeEventListener('webglcontextrestored', onContextRestored)
      document.removeEventListener('visibilitychange', onVisibilityChange)
      resized.disconnect()
      interacting = false
      // Detaches the pointer and wheel listeners the constructor attached, so a
      // viewer that is gone cannot be driven by events meant for the next one.
      controls?.dispose()
      controls = null
    })

    // Nothing to render: this component *is* the control, and the canvas around
    // it is TresJS's own element.
    return () => null
  },
})

//: A loader this component owns, rather than `useGLTF`: that composable keeps
//: no error the component can see, so a failed fetch could only ever look like
//: a slow one. Here the promise resolves with the scene or rejects with the cause.
//: `shallowRef` on purpose: a deep `ref` would wrap the three.js objects in Vue
//: proxies, and three.js's own read-only matrices then throw on every frame.
const model = shallowRef<GLTF | null>(null)
const loadError = ref<unknown>(null)
//: True while bytes are still arriving; once they are all in, what is left is
//: the decode, which is preparation rather than transfer.
const downloading = ref(true)
let unmounted = false

const draco = new DRACOLoader()
draco.setDecoderPath(DRACO_DECODER_PATH)
const gltfLoader = new GLTFLoader()
gltfLoader.setDRACOLoader(draco)

gltfLoader
  .loadAsync(props.url, (event) => {
    if (event.lengthComputable && event.loaded >= event.total) downloading.value = false
  })
  .then((gltf) => {
    if (!unmounted) model.value = gltf
  })
  .catch((cause: unknown) => {
    if (!unmounted) loadError.value = cause ?? new Error('model failed to load')
  })
  .finally(() => draco.dispose())

onBeforeUnmount(() => {
  unmounted = true
})

const cameraPosition = ref<[number, number, number]>([3, 2, 5])
const controlsTarget = ref<[number, number, number]>([0, 0, 0])
//: Half the model's largest extent, so the camera limits in `OrbitCamera` can be
//: expressed relative to the building rather than in absolute units.
const modelRadius = ref(1)
const rendererFailed = ref(false)

//: The loader's own verdict. A model that is merely slow has no error and stays
//: in the loading state.
const loadFailed = computed(() => !model.value && loadError.value !== null)
const failed = computed(() => rendererFailed.value || loadFailed.value)
const loading = computed(() => !model.value && !failed.value)

watch(loadFailed, (value) => {
  if (value) emit('failed')
})

//: Vertical field of view in degrees; reused by the fit maths below.
const FOV = 45

/**
 * Frame the whole model, once its scene is parsed.
 *
 * The camera sits above the ground (Z up), off to one side, far enough that the
 * whole bounding sphere fits the vertical field of view with some room to spare.
 */
function fitCamera(): void {
  const scene = model.value?.scene
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
    centre.y + distance * 0.75,
    centre.z + distance * 0.4,
  ]
  controlsTarget.value = [centre.x, centre.y, centre.z]
  modelRadius.value = radius
}

watch(model, (loaded) => {
  if (!loaded) return
  fitCamera()
})
</script>

<template>
  <div class="relative h-full w-full overflow-hidden bg-slate-950">
    <TresCanvas
      class="h-full w-full"
      clear-color="#0a1018"
      render-mode="on-demand"
      :antialias="true"
      :window-size="false"
      @error="rendererFailed = true"
    >
      <TresPerspectiveCamera :position="cameraPosition" :fov="FOV" :near="0.01" :far="500000" />
      <OrbitCamera
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
      <TresDirectionalLight :position="[3, 4, 5]" :intensity="2.2" />
      <TresDirectionalLight :position="[-4, -3, -3]" :intensity="0.9" />

      <primitive v-if="model?.scene" :object="model.scene" />
    </TresCanvas>

    <!-- Loading -->
    <div
      v-if="loading"
      class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-slate-950/90 text-slate-300"
    >
      <AppSpinner label="" class="scale-125" />
      <p class="text-sm">
        {{
          downloading
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
