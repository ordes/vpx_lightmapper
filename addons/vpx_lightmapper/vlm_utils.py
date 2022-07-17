#    Copyright (C) 2022  Vincent Bousquet
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>

import bpy
import array
import os
import re
import pathlib
import gpu
import math
import mathutils
import functools
import datetime
import string
import unicodedata
from mathutils import Vector
from gpu_extras.presets import draw_texture_2d
from gpu_extras.batch import batch_for_shader
from . import vlm_collections


def get_global_scale(context):
    if context.scene.vlmSettings.units_mode == 'vpx':
        return 0.01 # VPX units
    else:
        # 50 VP units = 1 1/16" ball = 1.0625" = 2.69875 cm
        return 0.01 * 2.69875 / 50 # Metric scale (imperial is performed by blender itself)


def get_lm_threshold():
    ''' 
    The lightmap influence threshold used for pruning uinfluenced lightmap faces (Face 
    with a max RGB channel value below this value will be pruned to limit mesh size and overdraw).
    '''
    #return 1.0 / (256.0 * 2.0) # Lowest threshold (2 * 1 bit precision) => 0.00195
    return 0.01


def get_render_size(context):
    opt_render_height = int(context.scene.vlmSettings.render_height)
    render_aspect_ratio = context.scene.vlmSettings.render_aspect_ratio
    render_size = (int(opt_render_height * render_aspect_ratio), opt_render_height)
    if context.scene.vlmSettings.layback_mode == 'fit_pf' and context.scene.vlmSettings.playfield_col:
        # render height apply to projected playfield, so upscale accordingly
        camera = get_vpx_item(context, 'VPX.Camera', 'Bake', single=True)
        if camera:
            winx = render_size[0] * context.scene.render.pixel_aspect_x
            winy = render_size[1] * context.scene.render.pixel_aspect_y
            min_u = min_v = 100000
            max_u = max_v = -100000
            for obj in context.scene.vlmSettings.playfield_col.all_objects:
                if obj.type == 'MESH':
                    mesh = obj.data
                    obj_mat = obj.matrix_basis
                    modelview_matrix = camera.matrix_world.normalized().inverted()
                    if winx > winy:
                        xasp = 1.0
                        yasp = winx / float(winy)
                    else:
                        xasp = winy / float(winx)
                        yasp = 1.0
                    shiftx = 0.5 - (camera.data.shift_x * xasp)
                    shifty = 0.5 - (camera.data.shift_y * yasp)
                    camsize = math.tan(camera.data.angle / 2.0)
                    uv_layer = mesh.uv_layers.active
                    for face in mesh.polygons:
                        y_mirror = 1.0
                        if obj.vlmSettings.is_spinner:
                            # Perform Y mirror for back facing spinner faces
                            normal = obj_mat @ face.normal
                            if normal.length_squared >= 0.5 and normal.dot(camera.location - face.center) <= 0.0: y_mirror = -1.0
                        for loop_idx in face.loop_indices:
                            co = mesh.vertices[mesh.loops[loop_idx].vertex_index].co
                            p1 = modelview_matrix @ obj_mat @ Vector((co[0], y_mirror * co[1], co[2], 1))
                            if p1.z == 0.0: p1.z = 0.00001
                            u = shiftx + xasp * (-p1.x * ((1.0 / camsize) / p1.z)) / 2.0
                            v = shifty + yasp * (-p1.y * ((1.0 / camsize) / p1.z)) / 2.0
                            min_v = min(min_v, v)
                            max_v = max(max_v, v)
                            min_u = min(min_u, u)
                            max_u = max(max_u, u)
            v_size = max_v - min_v
            if v_size > 0.0:
                s = 1.0 / v_size
                render_size = (int(s * opt_render_height * render_aspect_ratio), int(s * opt_render_height))
                print(f'. Upscale to fit PF to render size: {s}')
                print(f'. Expected playfield render size: {int((max_u-min_u)*render_size[0])}x{int((max_v-min_v)*render_size[1])}')
    return render_size
    

# 3D tri area ABC is half the length of AB cross product AC 
def tri_area(co1, co2, co3):
    return (co2 - co1).cross(co3 - co1).length / 2.0


# Adapted from Blender source code:
# winx/winy defines the render resolution, including pixel aspect ratio
# https://developer.blender.org/diffusion/B/browse/master/source/blender/editors/uvedit/uvedit_unwrap_ops.c
# https://developer.blender.org/diffusion/B/browse/master/source/blender/blenlib/intern/uvproject.c
def project_uv(camera, obj, winx=1.0, winy=1.0):
    if camera.type != 'CAMERA':
        raise Exception(f"Object {camera.name} is not a camera.")
    mesh = obj.data
    obj_mat = obj.matrix_basis
    modelview_matrix = camera.matrix_world.normalized().inverted()
    if winx > winy:
        xasp = 1.0
        yasp = winx / float(winy)
    else:
        xasp = winy / float(winx)
        yasp = 1.0
    shiftx = 0.5 - (camera.data.shift_x * xasp)
    shifty = 0.5 - (camera.data.shift_y * yasp)
    camsize = math.tan(camera.data.angle / 2.0)
    uv_layer = mesh.uv_layers.active
    for face in mesh.polygons:
        y_mirror = 1.0
        if obj.vlmSettings.is_spinner:
            # Perform Y mirror for back facing spinner faces
            normal = obj_mat @ face.normal
            if normal.length_squared >= 0.5 and normal.dot(camera.location - face.center) <= 0.0: y_mirror = -1.0
        for loop_idx in face.loop_indices:
            co = mesh.vertices[mesh.loops[loop_idx].vertex_index].co
            p1 = modelview_matrix @ obj_mat @ Vector((co[0], y_mirror * co[1], co[2], 1))
            if p1.z == 0.0: p1.z = 0.00001
            u = shiftx + xasp * (-p1.x * ((1.0 / camsize) / p1.z)) / 2.0
            v = shifty + yasp * (-p1.y * ((1.0 / camsize) / p1.z)) / 2.0
            uv_layer.data[loop_idx].uv = (u, v)
    
    
def fixSlash(filepath: str) -> str:
    """convert \\\+ to /"""
    filepath = re.sub(r"\\+", "/", filepath)
    filepath = re.sub(r"\/+", "/", filepath)
    return filepath
    

def get_assetlib_path():
    return fixSlash(os.path.dirname(__file__)) + "/assets/"
 
 
def get_library_path():
    #os.path.join(os.path.dirname(os.path.abspath(__file__)), "/assets/VPXMeshes.blend")
    return fixSlash(os.path.dirname(__file__)) + "/assets/VPXMeshes.blend"
 
 
def install_assetlib():
    shouldCreate = True
    for lib in bpy.context.preferences.filepaths.asset_libraries:
        if lib.path == get_assetlib_path():
            shouldCreate = False
    if shouldCreate:
        bpy.ops.preferences.asset_library_add(directory=get_assetlib_path())
        for lib in bpy.context.preferences.filepaths.asset_libraries:
            if lib.path == get_assetlib_path():
                lib.name = 'VLM Pinball Parts'


def uninstall_assetlib():
    libs = [(lib.name, lib.path) for lib in bpy.context.preferences.filepaths.asset_libraries if lib.path != get_assetlib_path()]
    for _ in range(len(bpy.context.preferences.filepaths.asset_libraries)):
        bpy.ops.preferences.asset_library_remove(0)
    for _, path in libs:
        bpy.ops.preferences.asset_library_add(directory=path)
    for lib in bpy.context.preferences.filepaths.asset_libraries:
        for name, path in libs:
            if lib.path == path:
                lib.name = name
                break
    if False: # Sadly this is not working as intended as Blender 3.2
        for index, lib in enumerate(bpy.context.preferences.filepaths.asset_libraries):
            if lib.path == get_assetlib_path():
                bpy.ops.preferences.asset_library_remove(index)
                break


def get_vpx_item(context, vpx_name, vpx_subpart, single=False):
    '''
    Search the complete scene for objects linked to the given vpx object/subpart
    Depending on single argument, returns a list of the found objects, eventually empty if none found
    or the first found element (or None if not found)
    '''
    if single:
        return next((o for o in context.scene.objects if vpx_name in o.vlmSettings.vpx_object.split(';') and o.vlmSettings.vpx_subpart == vpx_subpart), None)
    else:
        return [o for o in context.scene.objects if vpx_name in o.vlmSettings.vpx_object.split(';') and o.vlmSettings.vpx_subpart == vpx_subpart]


def load_library():
    """Append core meshes (without linking them in order to dispose the unused ones after import)
    and core shader node groups (with fake user to avoid loosing them)
    """
    librarypath = get_library_path()
    if not os.path.isfile(librarypath):
        print(f'{librarypath} does not exist')
    with bpy.data.libraries.load(librarypath, link=False) as (data_from, data_to):
        data_to.objects = [name for name in data_from.objects if name.startswith("VPX.Core.")]
        data_to.images = [name for name in data_from.images if name.startswith("VPX.Core.")]
        data_to.materials = [name for name in data_from.materials if name.startswith("VPX.Core.Mat.")]
        data_to.node_groups = ('VLM.BakeInfo', 'VPX.Material', 'VPX.Flasher', 'VPX.Light')


def clean_filename(filename):
    whitelist = "-_.() %s%s" % (string.ascii_letters, string.digits)
    
    # keep only valid ascii chars
    cleaned_filename = unicodedata.normalize('NFKD', filename).encode('ASCII', 'ignore').decode()
    
    # keep only whitelisted chars
    cleaned_filename = ''.join(c for c in cleaned_filename if c in whitelist)
    char_limit = 255
    if len(cleaned_filename)>char_limit:
        print("Warning, filename truncated because it was over {}. Filenames may no longer be unique".format(char_limit))
    return cleaned_filename[:char_limit]    


def push_render_settings(set_raw):
    state = (bpy.context.scene.render.use_border, bpy.context.scene.render.use_crop_to_border,
                bpy.context.scene.render.border_min_x, bpy.context.scene.render.border_max_x,
                bpy.context.scene.render.border_min_y, bpy.context.scene.render.border_max_y,
                bpy.context.scene.view_settings.view_transform,
                bpy.context.scene.view_settings.look,
                bpy.context.scene.render.pixel_aspect_x, bpy.context.scene.render.pixel_aspect_y,
                bpy.context.scene.render.engine,
                bpy.context.scene.render.film_transparent,
                bpy.context.scene.eevee.taa_render_samples,
                bpy.context.scene.render.image_settings.file_format,
                bpy.context.scene.render.image_settings.color_mode,
                bpy.context.scene.render.image_settings.color_depth,
                bpy.context.scene.cycles.samples,
                bpy.context.scene.cycles.use_denoising,
                bpy.context.scene.render.image_settings.exr_codec,
                bpy.context.scene.render.bake.use_clear,
                bpy.context.scene.render.bake.use_selected_to_active,
                bpy.context.scene.use_nodes,
                bpy.context.scene.render.resolution_x,
                bpy.context.scene.render.resolution_y,
                bpy.context.view_layer.use_pass_combined,
                bpy.context.view_layer.use_pass_object_index,
                bpy.context.scene.render.bake.cage_extrusion,
                bpy.context.scene.view_settings.exposure,
                bpy.context.scene.view_settings.gamma,
                )
    if set_raw:
        bpy.context.scene.view_settings.view_transform = 'Raw'
        bpy.context.scene.view_settings.look = 'None'
    return state


def pop_render_settings(state):
    bpy.context.scene.render.use_border = state[0]
    bpy.context.scene.render.use_crop_to_border = state[1]
    bpy.context.scene.render.border_min_x = state[2]
    bpy.context.scene.render.border_max_x = state[3]
    bpy.context.scene.render.border_min_y = state[4]
    bpy.context.scene.render.border_max_y = state[5]
    bpy.context.scene.view_settings.view_transform = state[6]
    bpy.context.scene.view_settings.look = state[7]
    bpy.context.scene.render.pixel_aspect_x = state[8]
    bpy.context.scene.render.pixel_aspect_y = state[9]
    bpy.context.scene.render.engine = state[10]
    bpy.context.scene.render.film_transparent = state[11]
    bpy.context.scene.eevee.taa_render_samples = state[12]
    bpy.context.scene.render.image_settings.file_format = state[13]
    bpy.context.scene.render.image_settings.color_mode = state[14]
    bpy.context.scene.render.image_settings.color_depth = state[15]
    bpy.context.scene.cycles.samples = state[16]
    bpy.context.scene.cycles.use_denoising = state[17]
    bpy.context.scene.render.image_settings.exr_codec = state[18]
    bpy.context.scene.render.bake.use_clear = state[19]
    bpy.context.scene.render.bake.use_selected_to_active = state[20]
    bpy.context.scene.use_nodes = state[21]
    bpy.context.scene.render.resolution_x = state[22]
    bpy.context.scene.render.resolution_y = state[23]
    bpy.context.view_layer.use_pass_combined = state[24]
    bpy.context.view_layer.use_pass_object_index = state[25]
    bpy.context.scene.render.bake.cage_extrusion = state[26]
    bpy.context.scene.view_settings.exposure = state[27]
    bpy.context.scene.view_settings.gamma = state[28]


def apply_split_normals(me):
	# Write the blender internal smoothing as custom split vertex normals
	me.calc_normals_split()
	cl_nors = array.array('f', [0.0] * (len(me.loops) * 3))
	me.loops.foreach_get('normal', cl_nors)
	me.polygons.foreach_set('use_smooth', [False] * len(me.polygons))
	nors_split_set = tuple(zip(*(iter(cl_nors),) * 3))
	me.normals_split_custom_set(nors_split_set)
	# Enable the use custom split normals data
	me.use_auto_smooth = True


def get_bakepath(context, type='ROOT'):
    if type == 'RENDERS':
        return f"//{os.path.splitext(bpy.path.basename(context.blend_data.filepath))[0]} - Bakes/Renders/"
    elif type == 'MASKS':
        return f"//{os.path.splitext(bpy.path.basename(context.blend_data.filepath))[0]} - Bakes/Object Masks/"
    elif type == 'EXPORT':
        return f"//{os.path.splitext(bpy.path.basename(context.blend_data.filepath))[0]} - Bakes/Export/"
    return f"//{os.path.splitext(bpy.path.basename(context.blend_data.filepath))[0]} - Bakes/"


def set_selected_and_active(context, obj):
    bpy.ops.object.select_all(action='DESELECT')
    context.view_layer.objects.active = obj
    obj.select_set(True)


def strip_vlm(name):
    if name.startswith('VLM.'):
        return name[4:]
    return name
    

def format_time(length_in_seconds):
    return str(datetime.timedelta(seconds=length_in_seconds)).split('.')[0]


def image_by_path(path):
    for image in bpy.data.images:
        if image.filepath == path:
            return image
    return None


def get_image_or_black(path, black_is_none=False):
    existing = image_by_path(path)
    if existing:
        return ('existing', existing)
    elif os.path.exists(bpy.path.abspath(path)):
        return ('loaded', bpy.data.images.load(path, check_existing=False))
    elif black_is_none:
        return ('black', None)
    else:
        black_image = bpy.data.images.get('VLM.NoTex')
        if not black_image:
            black_image = bpy.data.images.new('VLM.NoTex', 1, 1)
            black_image.generated_type = 'BLANK'
        return ('black', black_image)


def mkpath(path):
    pathlib.Path(bpy.path.abspath(path)).mkdir(parents=True, exist_ok=True)


def is_rgb_led(objects):
    n_objects = len(objects)
    if n_objects == 0 or not objects[0].vlmSettings.is_rgb_led:
        return False
    colors = [o.data.color for o in objects if o.type=='LIGHT']
    n_colors = len(colors)
    if n_colors != n_objects:
        print(f". Lights are marked as RGB Led but use colored emitter which are baked with their colors instead of full white (Lights: {[o.name for o in objects]}).")
        return True
    if n_objects == 1:
        return True
    base_color = functools.reduce(lambda a, b: (a[0]+b[0], a[1]+b[1], a[2]+b[2]), colors)
    base_color = (base_color[0] / n_colors, base_color[1] / n_colors, base_color[2] / n_colors)
    max_dif = max(map(lambda a: mathutils.Vector((a[0] - base_color[0], a[1] - base_color[1], a[2] - base_color[2])).length_squared, colors))
    threshold = 0.1
    if max_dif >= threshold * threshold:
        print(f". Lights are marked as RGB Led but use different colors (Lights: {[o.name for o in objects]}).")
    return True
        

def is_part_of_bake_category(obj, category):
    return next((col for col in obj.users_collection if col.vlmSettings.bake_mode == category), None) is not None


def get_lightings(context):
    """Return the list of lighting situations to be rendered as dictionary of tuples
        (name, is lightmap, light collection, lights, custom data)
    """
    light_scenarios = []
    light_cols = vlm_collections.get_collection(context.scene.collection, 'VLM.Lights', create=False)
    if light_cols is None: return light_scenarios
    for light_col in (l for l in light_cols.children if not l.hide_render):
        lights = light_col.all_objects
        if light_col.vlmSettings.light_mode == 'solid': # Base solid bake
            light_scenarios.append( [light_col.name, False, light_col, lights, None] )
        elif light_col.vlmSettings.light_mode == 'group': # Lightmap of a group of lights
            light_scenarios.append( [light_col.name, True, light_col, lights, None] )
        elif light_col.vlmSettings.light_mode == 'split': # Lightmaps for multiple VPX lights
            for vpx_lights in {tuple(sorted(set(l.vlmSettings.vpx_object.split(';')))) for l in lights}:
                light_group = [l for l in lights if tuple(sorted(set(l.vlmSettings.vpx_object.split(';')))) == vpx_lights]
                name = f"{light_col.name}-{vpx_lights[0]}"
                light_scenarios.append( [name, True, light_col, light_group, None] )
    # Sort by scenario name, starting by scenarios with custom world
    return sorted(light_scenarios, key=lambda scenario: f'0{scenario[0]}' if scenario[2].vlmSettings.world else f'1{scenario[0]}')


def get_n_lightings(context):
    return len(get_lightings(context))
    
    
def get_n_render_groups(context):
    n = 0
    bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
    if bake_col is not None:
        for obj in bake_col.all_objects:
            n = max(n, obj.vlmSettings.render_group + 1)
    return n


def render_mask(context, width, height, target_image, view_matrix, projection_matrix):
    """Uses Blender's internal renderer to render the active scene as an opacity mask
    to the given image (not saved)
    """
    offscreen = gpu.types.GPUOffScreen(width, height)
    area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
    space = area.spaces.active
    state = [
        space.overlay.show_floor,
        space.overlay.show_overlays,
        space.shading.background_type,
        space.shading.background_color,
        space.shading.light,
        space.shading.color_type,
        space.shading.single_color,
        space.shading.type,
        space.shading.render_pass
    ]
    space.overlay.show_floor = False
    space.overlay.show_overlays = False
    space.shading.background_type = 'VIEWPORT'
    space.shading.background_color = (0,0,0)
    space.shading.light = 'FLAT'
    space.shading.color_type = 'SINGLE'
    space.shading.single_color = (1,0, 0)
    space.shading.type = 'SOLID'
    with offscreen.bind():
        fb = gpu.state.active_framebuffer_get()
        fb.clear(color=(0.0, 0.0, 0.0, 0.0))
        offscreen.draw_view3d(
            context.scene,
            context.view_layer,
            space,
            area.regions[-1],
            view_matrix,
            projection_matrix,
            do_color_management=False)
        vertex_shader = '''
            in vec2 position;
            in vec2 uv;
            out vec2 uvInterp;
            void main() {
                uvInterp = uv;
                gl_Position = vec4(position, 0.0, 1.0);
            }
        '''
        bw_fragment_shader = '''
            uniform sampler2D image;
            in vec2 uvInterp;
            out vec4 FragColor;
            void main() {
                vec4 t = texture(image, uvInterp).rgba;
                FragColor = vec4(0.0, 0.0, 0.0, 2.1 * t.r);
            }
        '''
        bw_shader = gpu.types.GPUShader(vertex_shader, bw_fragment_shader)
        bw_shader.bind()
        bw_shader.uniform_sampler("image", offscreen.texture_color)
        batch_for_shader(
            bw_shader, 'TRI_FAN',
            {
                "position": ((-1, -1), (1, -1), (1, 1), (-1, 1)),
                "uv": ((0, 0), (1, 0), (1, 1), (0, 1)),
            },
        ).draw(bw_shader)
        buffer = gpu.state.active_framebuffer_get().read_color(0, 0, width, height, 4, 0, 'UBYTE')
    offscreen.free()
    space.overlay.show_floor = state[0]
    space.overlay.show_overlays = state[1]
    space.shading.background_type = state[2]
    space.shading.background_color = state[3]
    space.shading.light = state[4]
    if state[5] != '':
        space.shading.color_type = state[5]
    space.shading.single_color = state[6]
    space.shading.type = state[7]
    
    target_image.scale(width, height)
    buffer.dimensions = width * height * 4
    target_image.pixels = [v / 255 for v in buffer]
