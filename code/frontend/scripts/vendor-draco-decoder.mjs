/**
 * Copy Three.js's Draco decoder into `public/draco/`, where the 3D viewer can
 * fetch it from the app's own origin.
 *
 * Why this is needed at all: ODM's `--gltf` export compresses the mesh with
 * `KHR_draco_mesh_compression`, and `GLTFLoader` refuses such a file unless a
 * `DRACOLoader` is attached ("No DRACOLoader instance provided"). The decoder
 * is not a module that can be imported and bundled - `DRACOLoader` loads it as
 * a file (a wrapper script plus a `.wasm` binary, or the pure-JS fallback) and
 * starts a worker from it - so the files have to exist under a URL prefix and
 * be handed over with `setDecoderPath('/draco/')`.
 *
 * Vendored instead of pointing at Google's CDN, which is the loader's default:
 * this deployment is entirely local (frames, meshes, API), and a viewer that
 * needs the public internet to draw a mesh would fail on exactly the machines
 * the rest of the stack was built to run on.
 *
 * Runs before `dev` and `build`; the destination is generated, so it is
 * git-ignored. The source files belong to the installed `three`, which is what
 * keeps the decoder version matched to the loader.
 */

import { copyFileSync, existsSync, mkdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const source = join(root, 'node_modules/three/examples/jsm/libs/draco/gltf')
const destination = join(root, 'public/draco')

//: The files `DRACOLoader` asks for: wrapper + binary on any browser with
//: WebAssembly, the self-contained JS decoder as the fallback.
const FILES = ['draco_wasm_wrapper.js', 'draco_decoder.wasm', 'draco_decoder.js']

mkdirSync(destination, { recursive: true })

for (const file of FILES) {
  const from = join(source, file)
  if (!existsSync(from)) {
    console.error(`error: ${from} is missing - is "three" installed?`)
    process.exit(1)
  }
  copyFileSync(from, join(destination, file))
}

console.log(`draco decoder ready: public/draco (${FILES.length} files)`)
