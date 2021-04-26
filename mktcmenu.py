#!/usr/bin/env python3

import contextlib
import functools
import re
import itertools
import argparse
import os
import io
import copy
from collections import UserDict
from typing import Optional, Sequence, Any, IO

# TODO is this actually safe?
import yaml
try:
    from yaml import CLoader as Loader, CDumper as Dumper
except ImportError:
    from yaml import SafeLoader as Loader, SafeDumper as Dumper

yaml_load = functools.partial(yaml.load, Loader=Loader)
yaml_dump = functools.partial(yaml.dump, Dumper=Dumper, default_flow_style=False)

RE_AUTOID_DELIM = re.compile(r'[\W_]+')
RE_CPP_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

DESC_SUFFIX = '.tcmdesc.yaml'
MAP_SUFFIX = '.tcmmap.yaml'

SRC_HEADER = '''
/**
 * Automatically managed by mktcmenu.
 *
 * DO NOT manually edit this file. Changes made in this file will be overwritten
 * on next descriptor generation.
 */
'''.lstrip()


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('desc', help='Menu descriptor file (*.tcmdesc.yaml).')
    p.add_argument('-e', '--eeprom-map', help='Override EEPROM mapping file location (defaults to <descriptor basename without suffix>.tcmmap.yaml).')
    p.add_argument('-c', '--eeprom-capacity', type=int, help='Set EEPROM capacity (only used during initialization/defragmentation of the mapping file).')
    p.add_argument('-o', '--output-dir', help='Output directory (defaults to <descriptor dirname>/gen).')
    p.add_argument('-s', '--source-dir', default='.', help='C++ source directory (defaults to .).')
    p.add_argument('-i', '--include-dir', default='.', help='Include directory (defaults to .).')
    p.add_argument('-p', '--pgmspace', action='store_true', default=False, help='Enable pgmspace support for some Arduino platforms (e.g. avr8 and esp8266).')
    return p, p.parse_args()

# C++ code emitter helpers
def emit_cppdef(buf, name, type_, is_static=False, is_const=False, is_constexpr=False, is_extern=False, nmemb=-1, init=False, extra_decl=tuple()):
    extern_kw = 'extern ' if is_extern else ''
    static_kw = 'static ' if is_static else ''
    const_kw = 'const ' if is_const else ''
    constexpr_kw = 'constexpr ' if is_constexpr else ''
    extra_decl_str = f' {" ".join(extra_decl)}' if len(extra_decl) != 0 else ''
    if nmemb < 0:
        nmemb_str = ''
    elif nmemb == 0:
        nmemb_str = '[]'
    else:
        nmemb_str = f'[{nmemb}]'
    buf.write(f'{extern_kw}{static_kw}{constexpr_kw}{const_kw}{type_} {name}{nmemb_str}{extra_decl_str}{" = " if init else ""}')

def emit_cppeol(buf):
    buf.write(';\n')

@contextlib.contextmanager
def emit_cppobjarray(buf, multiline=False):
    buf.write('{')
    buf.write('\n' if multiline else ' ')
    try:
        yield buf
    finally:
        buf.write('\n' if multiline else ' ')
        buf.write('}')

def emit_cppindent(buf, level=1):
    buf.write('    ' * level)

def cppstr(str_):
    str_escaped = str(str_).replace('"', r'\"')
    return f'"{str_escaped}"'


class EEPROMMap(UserDict):
    def __init__(self, capacity=0xffff, reserve=0):
        super().__init__()
        super().__setitem__('_reserved', {'offset': 0, 'size': 2})
        self._auto_index = 2
        self.capacity = capacity
        self.varstore_bar = self.capacity - reserve
        self.spare_segments = {}

    @property
    def auto_index(self):
        return self._auto_index

    def auto_allocate(self, name, size):
        max_space = min(self.varstore_bar, self.capacity)
        offset = self._auto_index
        if offset >= 0xffff or offset+size > 0xffff:
            raise RuntimeError('EEPROM address space exhausted. Please run defragmentation and bump EEPROM mapping version.')
        elif offset >= max_space or offset+size >= max_space:
            raise RuntimeError('No space left on EEPROM. Please run defragmentation and bump EEPROM mapping version.')
        allocated = {'offset': offset, 'size': size}
        super().__setitem__(name, allocated)
        self._auto_index += size
        return allocated

    def check_consistency(self):
        pass # TODO perform intersection to find holes/overlaps/oob allocations

    @classmethod
    def load(cls, fmap: IO[str]):
        data = yaml_load(fmap)
        obj = cls()
        obj.capacity = data['capacity']
        obj.varstore_bar = data['varstore-bar']
        obj._auto_index = data['auto-index']
        if 'vars' in data:
            obj.data.clear()
            obj.data.update(data['vars'])
        if 'spare-segments' in data:
            obj.spare_segments.update(data['spare-segments'])
        return obj

    def save(self, fmap: IO[str]):
        data = {
            'capacity': self.capacity,
            'varstore-bar': self.varstore_bar,
            'auto-index': self._auto_index,
            'vars': self.data,
        }
        if len(self.spare_segments) != 0:
            data['spare-segments'] = self.spare_segments
        yaml_dump(data, fmap)

# Data model for menu entries 
class MenuBaseType:
    auto_index = 1
    serializable = False
    cpp_type_prefix = ''
    render_callback_parent = ''
    def __init__(self, props, alias):
        v = functools.partial(self._validate_entry, props)
        self._global_index = MenuBaseType.auto_index
        MenuBaseType.auto_index += 1
        self.id_ = v('id')
        self.id_suffix = v('id-suffix')
        self.name = v('name', required=True)
        self.persistent = v('persistent', default=False)
        self.read_only = v('read-only', default=False)
        self.local_only = v('local-only', default=False)
        self.visible = v('visible', default=True)
        self.callback = v('callback')

    @staticmethod
    def _validate_entry(props, key, required=False, default=None, extra_validation=None):
        if required and key not in props:
            raise ValueError(f'Required property {key} is missing.')
        if required:
            value = props[key]
        else:
            value = props.get(key, default)
        if extra_validation is not None:
            extra_validation(value)
        return value

    def emit_code(self, ctx: 'CodeEmitterContext'):
        raise NotImplementedError()

    def get_serialized_size(self):
        raise NotImplementedError()

    def get_type_name(self):
        raise NotImplementedError()

    def emit_default_flags_block(self, buf, namespace: Sequence["MenuBaseType"]):
        id_ = self.generate_id()
        ns_id = ''.join(ns.generate_id() for ns in namespace)
        menu_name = f'menu{ns_id}{id_}'
        emit_cppindent(buf, level=1)
        if self.read_only:
            buf.write(f'{menu_name}.setReadOnly(true);')
        if self.local_only:
            buf.write(f'{menu_name}.setLocalOnly(true);')
        if not self.visible:
            buf.write(f'{menu_name}.setVisible(false);')

    def emit_simple_static_menu_item(self, ctx: 'CodeEmitterContext', minfo_extra: Sequence[Any], menu_item_extra: Sequence[Any], cpp_type_prefix: Optional[str] = None, cpp_type_prefix_minfo: Optional[str] = None, next_entry_namespace: Sequence["MenuBaseType"] = None):
        eeprom_offset = self.find_or_allocate_eeprom_space(ctx.eeprom_map)

        id_ = self.generate_id()
        ns_id = ''.join(ns.generate_id() for ns in ctx.namespace)
        if next_entry_namespace is None:
            next_ns_id = ns_id
        else:
            next_ns_id = ''.join(ns.generate_id() for ns in next_entry_namespace)

        minfo_name = f'minfo{ns_id}{id_}'
        menu_name = f'menu{ns_id}{id_}'

        cpp_type_prefix = self.__class__.cpp_type_prefix if cpp_type_prefix is None else cpp_type_prefix
        cpp_type_prefix_minfo = cpp_type_prefix if cpp_type_prefix_minfo is None else cpp_type_prefix_minfo
        minfo_type = f'{cpp_type_prefix_minfo}MenuInfo'
        menu_type = f'{cpp_type_prefix}MenuItem'

        next_name = f'menu{next_ns_id}{ctx.next_entry.generate_id()}' if ctx.next_entry is not None else None
        next_name_ref = f'&{next_name}' if next_name is not None else 'nullptr'

        minfo_builtin = (cppstr(self.name), self._global_index, hex(eeprom_offset),)
        menu_item_first = (f'&{minfo_name}',)
        menu_item_last = (next_name_ref,)

        emit_cppdef(ctx.bufsrc, minfo_name, minfo_type, is_const=True, is_static=True, extra_decl=('PROGMEM', ) if ctx.use_pgmspace else tuple(), init=True)
        with emit_cppobjarray(ctx.bufsrc):
            ctx.bufsrc.write(', '.join(map(str, itertools.chain(minfo_builtin, minfo_extra))))
        emit_cppeol(ctx.bufsrc)

        emit_cppdef(ctx.bufsrc, menu_name, menu_type)
        ctx.bufsrc.write(f'({", ".join(map(str, itertools.chain(menu_item_first, menu_item_extra, menu_item_last)))})')
        emit_cppeol(ctx.bufsrc)
        ctx.bufsrc.write('\n')

        emit_cppdef(ctx.bufhdr, menu_name, menu_type, is_extern=True)
        emit_cppeol(ctx.bufhdr)

        return menu_name

    def emit_simple_dynamic_menu_item(self, ctx: 'CodeEmitterContext', menu_item_extra: Sequence[Any], name_prefix: Optional[str] = None, cpp_type_prefix: Optional[str] = None, render_callback_parent: Optional[str] = None, global_index_order: bool = 'after_callback', next_entry_namespace: Sequence["MenuBaseType"] = None, custom_callback_ref: Optional[str] = None):
        # global_index_order: first, after_callback, na
        eeprom_offset = self.find_or_allocate_eeprom_space(ctx.eeprom_map)

        id_ = self.generate_id()
        ns_id = ''.join(ns.generate_id() for ns in ctx.namespace)
        if next_entry_namespace is None:
            next_ns_id = ns_id
        else:
            next_ns_id = ''.join(ns.generate_id() for ns in next_entry_namespace)

        menu_name = f'menu{name_prefix or ""}{ns_id}{id_}'
        if custom_callback_ref is None:
            render_callback_name = f'fn{ns_id}{id_}RtCall'
        else:
            render_callback_name = custom_callback_ref

        cpp_type_prefix = self.__class__.cpp_type_prefix if cpp_type_prefix is None else cpp_type_prefix
        render_callback_parent = self.__class__.render_callback_parent if render_callback_parent is None else render_callback_parent

        menu_type = f'{cpp_type_prefix}MenuItem'

        next_name = f'menu{next_ns_id}{ctx.next_entry.generate_id()}' if ctx.next_entry is not None else None
        next_name_ref = f'&{next_name}' if next_name is not None else 'nullptr'

        if global_index_order == 'after_callback':
            menu_item_first = (render_callback_name, self._global_index, )
        elif global_index_order == 'first':
            menu_item_first = (self._global_index, render_callback_name, )
        elif global_index_order == 'na':
            menu_item_first = (render_callback_name, )
        else:
            raise ValueError(f'Invalid global_index_order {global_index_order}')

        menu_item_last = (next_name_ref, )

        if custom_callback_ref is None:
            callback_factory_params = ', '.join(map(str, (
                render_callback_name, render_callback_parent,
                cppstr(self.name), hex(eeprom_offset), self.get_callback_ref()
            )))
            ctx.bufsrc.write(f'RENDERING_CALLBACK_NAME_INVOKE({callback_factory_params})\n')

        emit_cppdef(ctx.bufsrc, menu_name, menu_type)
        ctx.bufsrc.write(f'({", ".join(map(str, itertools.chain(menu_item_first, menu_item_extra, menu_item_last)))})')
        emit_cppeol(ctx.bufsrc)
        ctx.bufsrc.write('\n')

        emit_cppdef(ctx.bufhdr, menu_name, menu_type, is_extern=True)
        emit_cppeol(ctx.bufhdr)

        return menu_name

    def get_callback_ref(self):
        return 'NO_CALLBACK' if self.callback is None or len(self.callback) == 0 else f'{self.callback}'

    def generate_id(self):
        if self.id_ is not None:
            id_ = self.id_
        else:
            id_ = ''.join(w.capitalize() for w in RE_AUTOID_DELIM.split(self.name))
        #id_ = f'{id_}{self.get_type_name()}{self.id_suffix if self.id_suffix is not None else ""}'
        id_ = f'{id_}{self.id_suffix if self.id_suffix is not None else ""}'
        return id_

    def find_or_allocate_eeprom_space(self, eeprom_map: EEPROMMap):
        id_ = self.generate_id()
        if self.__class__.serializable and self.persistent and id_ in eeprom_map:
            offsize = eeprom_map[id_]
            if offsize['size'] == self.get_serialized_size():
                return offsize['offset']
            else:
                # TODO maybe give a warning about this?
                del eeprom_map[id_]
                new_offsize = eeprom_map.auto_allocate(id_, self.get_serialized_size())
                return new_offsize['offset']
        elif self.persistent:
            offsize = eeprom_map.auto_allocate(id_, self.get_serialized_size())
            return offsize['offset']
        else:
            return 0xffff

    def list_callbacks(self):
        return {('on_change', self.callback)} if self.callback is not None else set()


class CodeEmitterContext:
    def __init__(self, bufsrc: IO[str], bufhdr: IO[str], eeprom_map: EEPROMMap, namespace: Sequence[MenuBaseType], next_entry: MenuBaseType, use_pgmspace: bool):
        self.bufsrc = bufsrc
        self.bufhdr = bufhdr
        self.eeprom_map = eeprom_map
        self.namespace = namespace
        self.next_entry = next_entry
        self.use_pgmspace = use_pgmspace


class AnalogType(MenuBaseType):
    serializable = True
    cpp_type_prefix = 'Analog'
    def __init__(self, props, alias):
        super().__init__(props, alias)
        v = functools.partial(self._validate_entry, props)
        max_ = v('max', default=None)
        min_ = v('min', default=None)
        self.precision = v('precision', default=None)
        self.offset = v('offset', default=None)
        self.divisor = v('divisor', default=1)
        self.unit = v('unit')
        if self.offset is None and min_ is None:
            self.offset = 0
        elif self.offset is None:
            self.offset = min_
        elif self.offset is not None and min_ is not None:
            raise ValueError('Offset and min are mutually exclusive.')

        if self.precision is None and max_ is None:
            raise ValueError(f'One of precision or max must be specified.')
        elif self.precision is None:
            self.precision = max_ - self.offset
        elif self.precision is not None and max_ is not None:
            raise ValueError('Precision and max are mutually exclusive.')

    def get_serialized_size(self):
        return 2

    def get_type_name(self):
        return 'I'

    def emit_code(self, ctx: CodeEmitterContext):
        self.emit_simple_static_menu_item(ctx, (
            self.precision, self.get_callback_ref(), self.offset, self.divisor,
            cppstr(self.unit) if self.unit is not None else cppstr(""),
        ), (
            0,
        ))

class LargeNumberType(MenuBaseType):
    serializable = True
    cpp_type_prefix = 'EditableLargeNumber'
    render_callback_parent = 'largeNumItemRenderFn'
    def __init__(self, props, alias):
        super().__init__(props, alias)
        v = functools.partial(self._validate_entry, props)
        self.decimal_places = v('decimal-places', default=0)
        self.length = v('length', default=12)
        self.signed = v('signed', default=False)

    def get_serialized_size(self):
        # TODO is this 7 or 8?
        # https://github.com/davetcc/tcMenuLib/blob/3d4ae0621df020c3919e3512a5c33b9b5a1cef6f/src/EepromItemStorage.cpp#L37-L41
        # The source shows 7 (sign byte+12 nibbles) but the editor shows 8
        return 7

    def get_type_name(self):
        return f'LN'

    def emit_code(self, ctx: CodeEmitterContext):
        self.emit_simple_dynamic_menu_item(ctx, (
            self.length, self.decimal_places, str(self.signed).lower(),
        ), global_index_order='after_callback')


class FloatType(MenuBaseType):
    cpp_type_prefix = 'Float'
    def __init__(self, props, alias):
        super().__init__(props, alias)
        v = functools.partial(self._validate_entry, props)
        self.decimal_places = v('decimal-places', default=2)

    def get_serialized_size(self):
        raise ValueError('FloatType is not serializable')

    def get_type_name(self):
        return f'F'

    def emit_code(self, ctx: CodeEmitterContext):
        self.emit_simple_static_menu_item(ctx, (
            self.decimal_places, self.get_callback_ref()
        ), tuple())

class EnumType(MenuBaseType):
    serializable = True
    cpp_type_prefix = 'Enum'
    def __init__(self, props, alias):
        super().__init__(props, alias)
        v = functools.partial(self._validate_entry, props)
        self.options = v('options', required=True)

    def get_serialized_size(self):
        return 2

    def get_type_name(self):
        return 'E'

    def emit_code(self, ctx: CodeEmitterContext):
        ns_id = ''.join(ns.generate_id() for ns in ctx.namespace)
        enum_str_name = f'enumStr{ns_id}{self.generate_id()}'

        # Write enum item strings
        for i, str_ in enumerate(self.options):
            emit_cppdef(ctx.bufsrc, f'{enum_str_name}_{i}', 'char', is_const=True, is_static=True, nmemb=0, init=True, extra_decl=('PROGMEM', ) if ctx.use_pgmspace else tuple())
            ctx.bufsrc.write(cppstr(str_))
            emit_cppeol(ctx.bufsrc)

        nmemb = len(self.options)
        emit_cppdef(ctx.bufsrc, enum_str_name, 'char * const', is_const=True, is_static=True, nmemb=nmemb, init=True, extra_decl=('PROGMEM', ) if ctx.use_pgmspace else tuple())
        with emit_cppobjarray(ctx.bufsrc, multiline=True):
            ctx.bufsrc.write(',\n'.join(f'    {enum_str_name}_{i}' for i in range(nmemb)))
        emit_cppeol(ctx.bufsrc)

        # ew
        self.emit_simple_static_menu_item(ctx, (
            nmemb - 1, self.get_callback_ref(), enum_str_name,
        ) ,(0, ))

class ScrollChoiceType(MenuBaseType):
    serializable = True
    cpp_type_prefix = 'ScrollChoice'
    render_callback_parent = 'enumItemRenderFn'
    def __init__(self, props, alias):
        super().__init__(props, alias)
        v = functools.partial(self._validate_entry, props)
        self.item_size = v('item-size', required=True)
        self.items = v('items', required=True)
        self.data_source = v('data-source', required=True, extra_validation=self._validate_data_source)
        self._mode, self._address = self.data_source.split(':')

    def get_serialized_size(self):
        return 2

    def get_type_name(self):
        return 'SC'

    @staticmethod
    def _validate_data_source(ds):
        _valid_entry = ('eeprom', 'array-in-eeprom', 'ram', 'array-in-ram', 'custom-renderfn')
        ds_split = ds.split(':')
        if len(ds_split) != 2:
            raise ValueError(f'Invalid ScrollChoiceType data-source {ds} (more than 1 delimiter)')
        mode, _address = ds_split
        if mode not in _valid_entry:
            raise ValueError(f'Invalid ScrollChoiceType mode {mode} (expecting one of {_valid_entry})')


    def emit_code(self, ctx: CodeEmitterContext):
        if self._mode in ('eeprom', 'array-in-eeprom'):
            custom_callback = None
            menu_item_extra = (0, ctx.eeprom_map.spare_segments[self._address], self.item_size, self.items)
        elif self._mode in ('ram', 'array-in-ram'):
            custom_callback = None
            menu_item_extra = (0, self._address, self.item_size, self.items)
            emit_cppdef(ctx.bufsrc, self._address, 'char *', is_const=True, is_extern=True)
            emit_cppeol(ctx.bufsrc)
        else:
            custom_callback = self._address
            menu_item_extra = (0, self.items)
        self.emit_simple_dynamic_menu_item(ctx,
                                           menu_item_extra, global_index_order='first',
                                           custom_callback_ref=custom_callback)

    def list_callbacks(self):
        result = super().list_callbacks()
        if self._mode == 'custom-renderfn':
            result.add(('on_render', self._address))
        return result

class BooleanType(MenuBaseType):
    serializable = True
    cpp_type_prefix = 'Boolean'
    def __init__(self, props, alias):
        super().__init__(props, alias)
        v = functools.partial(self._validate_entry, props)

        _default = {
            'boolean': 'true-false',
            'bool': 'true-false',
            'truefalse': 'true-false',
            'switch': 'on-off',
            'onoff': 'on-off',
            'yesno': 'yes-no'
        }
        self.response = v('response', default=_default[alias], extra_validation=self._validate_response)

    def get_serialized_size(self):
        return 1

    def get_type_name(self):
        return 'B'

    def emit_code(self, ctx: CodeEmitterContext):
        _response_syms = {
            'true-false': 'NAMING_TRUE_FALSE',
            'on-off': 'NAMING_ON_OFF',
            'yes-no': 'NAMING_YES_NO',
        }
        self.emit_simple_static_menu_item(ctx, (
            1, self.get_callback_ref(), _response_syms[self.response],
        ) ,('false', ))

    @staticmethod
    def _validate_response(response):
        _valid_entry = ('true-false', 'yes-no', 'on-off')
        if response not in _valid_entry:
            raise ValueError(f'Invalid ScrollChoiceType response {response} (expecting one of {_valid_entry})')


class SubMenuType(MenuBaseType):
    cpp_type_prefix = 'Sub'
    def __init__(self, props, alias):
        super().__init__(props, alias)
        v = functools.partial(self._validate_entry, props)
        self.items = v('items', required=True)
        self.auth = v('auth', default=False)

    def get_serialized_size(self):
        raise ValueError('SubMenuType is not serializable')

    def get_type_name(self):
        return f'M'

    def emit_code(self, ctx: CodeEmitterContext):
        # TODO
        subctx = copy.copy(ctx)
        subctx.namespace = ctx.namespace + (self, )
        for i, subitem in enumerate(self.items):
            subctx.next_entry = self.items[i+1] if len(self.items) > i+1 else None
            subitem.emit_code(subctx)
        backctx = copy.copy(ctx)
        backctx.next_entry = self.items[0]
        back_name = self.emit_simple_dynamic_menu_item(
            backctx,
            tuple(),
            # Try to avoid name collision
            name_prefix='back',
            cpp_type_prefix='Back',
            render_callback_parent='backSubItemRenderFn',
            global_index_order='na',
            next_entry_namespace=subctx.namespace,
        )
        self.emit_simple_static_menu_item(ctx, (
            0, self.get_callback_ref(),
        ), (f'&{back_name}', ))

    def list_callbacks(self):
        callback_list = super().list_callbacks()
        for item in self.items:
            callback_list.update(item.list_callbacks())
        return callback_list


class ActionType(MenuBaseType):
    cpp_type_prefix = 'Action'
    def get_serialized_size(self):
        raise ValueError('ActionType is not serializable')

    def emit_code(self, ctx: CodeEmitterContext):
        # seriously having a codegen is not an excuse for inconsistent API design
        self.emit_simple_static_menu_item(ctx, (
            0, self.get_callback_ref(),
        ), tuple(), cpp_type_prefix_minfo='Any')

YAML_TAG_SUFFIXES = {
    'analog': AnalogType,
    'fixed': AnalogType,
    'number': AnalogType,

    'large-number': LargeNumberType,
    'bcd': LargeNumberType,

    'float': FloatType,

    'enum': EnumType,
    'option': EnumType,
    'static-option': EnumType,

    'scroll-choice': ScrollChoiceType,
    'scroll': ScrollChoiceType,
    'dynamic-option': ScrollChoiceType,

    'boolean': BooleanType,
    'bool': BooleanType,
    'truefalse': BooleanType,
    'switch': BooleanType,
    'onoff': BooleanType,
    'yesno': BooleanType,

    'submenu': SubMenuType,
    'menu': SubMenuType,

    'action': ActionType,

    # 'programmable-menu': ListType,
    # 'list': ListType,

    # 'multi-part': MultiPartType,
    # 'struct': MultiPartType,
    # 'str': MultiPartType,
    # 'ipv4': MultiPartType,
    # 'time-24h': MultiPartType,
    # 'time-12h': MultiPartType,
    # 'date': MultiPartType,

    # 'color': ColorType,
    # 'rgb': ColorType,
    # 'rgba': ColorType,
}

def tcdesc_multi_constructor(loader: yaml.Loader, tag_suffix, node):
    if tag_suffix in YAML_TAG_SUFFIXES:
        node_parsed = loader.construct_mapping(node)
    else:
        raise RuntimeError(f'Unknown TCMenu menu entry type {tag_suffix}')
    return YAML_TAG_SUFFIXES[tag_suffix](node_parsed, alias=tag_suffix)

yaml.add_multi_constructor('!tcm/', tcdesc_multi_constructor, Loader=Loader)

# TODO change paths to path-like?
def do_codegen(desc_path: str, out_dir: str, source_dir: str, include_dir: str, instance_name: str, eeprom_map: EEPROMMap, use_pgmspace: bool):
    full_source_dir = os.path.normpath(os.path.join(out_dir, source_dir))
    full_include_dir = os.path.normpath(os.path.join(out_dir, include_dir))
    os.makedirs(full_source_dir, exist_ok=True)
    if full_source_dir != full_include_dir:
        os.makedirs(full_include_dir, exist_ok=True)

    menu_header_name = f'{instance_name}.h'
    menu_source_name = f'{instance_name}_desc.cpp'
    callback_header_name = f'{instance_name}_callback.h'
    extra_header_name = f'{instance_name}_extra.h'

    menu_header_path = os.path.join(out_dir, include_dir, menu_header_name)
    menu_source_path = os.path.join(out_dir, source_dir, menu_source_name)
    callback_header_path = os.path.join(out_dir, include_dir, callback_header_name)
    extra_header_path = os.path.join(out_dir, include_dir, extra_header_name)

    with open(desc_path, 'r') as f:
        desc = yaml_load(f)
    bufsrc = io.StringIO()
    bufhdr = io.StringIO()

    namespace = tuple()
    callback_list = set()

    with open(menu_source_path, 'w') as bufsrc, open(menu_header_path, 'w') as bufhdr:
        # Output header
        bufsrc.write(SRC_HEADER)
        bufhdr.write(SRC_HEADER)
        bufsrc.write('\n')
        bufhdr.write('\n')

        # Output includes
        if use_pgmspace:
            bufsrc.write('#include <Arduino.h>\n')
        bufsrc.write('#include <tcMenu.h>\n')
        bufsrc.write(f'#include "{menu_header_name}"\n\n')

        bufhdr.write('#pragma once\n')
        bufhdr.write('#include <tcMenu.h>\n\n')
        bufhdr.write(f'#include "{callback_header_name}"\n')
        bufhdr.write(f'#include "{extra_header_name}"\n\n')

        # Output application info
        emit_cppdef(bufsrc, 'applicationInfo', 'ConnectorLocalInfo', is_const=True, extra_decl=('PROGMEM', ) if use_pgmspace else tuple(), init=True)
        with emit_cppobjarray(bufsrc):
            bufsrc.write(f'{cppstr(desc["name"])}, {cppstr(desc["uuid"])}')
        emit_cppeol(bufsrc)
        bufsrc.write('\n')

        emit_cppdef(bufhdr, 'applicationInfo', 'ConnectorLocalInfo', is_const=True, is_extern=True)
        emit_cppeol(bufhdr)

        ctx = CodeEmitterContext(bufsrc, bufhdr, eeprom_map, namespace, None, use_pgmspace)
        # Output menu descriptor
        for i, item in enumerate(desc["items"]):
            ctx.next_entry = desc["items"][i+1] if len(desc["items"]) > i+1 else None
            item.emit_code(ctx)
            callback_list.update(item.list_callbacks())

        # Define a getter for the root of menu descriptor
        bufhdr.write(f'constexpr MenuItem *getRootMenuItem() {{ return &menu{desc["items"][0].generate_id()}; }}\n')

        bufhdr.write('\n')

        # Define menu property initializer
        emit_cppdef(bufsrc, 'setupMenuDefaults', 'void')
        bufsrc.write('() ')
        with emit_cppobjarray(bufsrc, multiline=True):
            for item in desc["items"]:
                item.emit_default_flags_block(bufsrc, namespace)

        emit_cppdef(bufhdr, 'setupMenuDefaults', 'void')
        bufhdr.write('()')
        emit_cppeol(bufhdr)

    # Generate callback header
    with open(callback_header_path, 'w') as bufcb:
        bufcb.write(SRC_HEADER)
        bufcb.write('\n')

        bufcb.write('#pragma once\n')
        bufcb.write('#include <tcMenu.h>\n')
        bufcb.write('#include <stdint.h>\n\n')

        callback_overlap_check = {}
        for cb_type, cb_ref in callback_list:
            if cb_ref in callback_overlap_check:
                raise RuntimeError(f'Callback {cb_ref} conflicts with other callbacks.')
            callback_overlap_check[cb_ref] = cb_type
            if cb_type == 'on_change':
                bufcb.write(f'void {cb_ref}(int id);\n')
            elif cb_type == 'on_render':
                bufcb.write(f'int {cb_ref}(RuntimeMenuItem* item, uint8_t row, RenderFnMode mode, char* buffer, int bufferSize);\n')

    with open(extra_header_path, 'w') as bufext:
        # TODO: Make this dynamic?
        bufext.write(SRC_HEADER)
        bufext.write('\n')

        bufext.write('#pragma once\n')
        bufext.write('#include <ScrollChoiceMenuItem.h>\n')
        bufext.write('#include <EditableLargeNumberMenuItem.h>\n')

if __name__ == '__main__':
    p, args = parse_args()
    desc_dirname, desc_basename = os.path.split(args.desc)

    is_standard_suffix = len(desc_basename) > len(DESC_SUFFIX) and desc_basename.endswith(DESC_SUFFIX)
    desc_instance_name = desc_basename[:-len(DESC_SUFFIX)] if is_standard_suffix else os.path.splitext(desc_basename)[0]

    out_dir = args.output_dir if args.output_dir is not None else os.path.join(desc_dirname, 'gen')

    if args.eeprom_map is not None:
        eeprom_map_file = args.eeprom_map
    else:
        eeprom_map_file = os.path.join(desc_dirname, f'{desc_instance_name}{MAP_SUFFIX}')

    if os.path.isfile(eeprom_map_file):
        with open(eeprom_map_file, 'r') as f:
            eeprom_map = EEPROMMap.load(f)
        if args.eeprom_capacity is not None and args.eeprom_capacity != eeprom_map.capacity:
            print('WARNING: Ignoring --eeprom-capacity and using the capacity specified in the mapping file.')
    else:
        if args.eeprom_capacity is None:
            p.error('--eeprom-capacity must be specified when initializing the mapping file.')
        eeprom_map = EEPROMMap(args.eeprom_capacity)

    do_codegen(args.desc, out_dir, args.source_dir, args.include_dir, desc_instance_name, eeprom_map, args.pgmspace)

    with open(eeprom_map_file, 'w') as f:
        eeprom_map.save(f)
