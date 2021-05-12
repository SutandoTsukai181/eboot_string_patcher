#!/usr/bin/env python

__author__ = 'SutandoTsukai181'
__version__ = '1.0'

import json
import os
from argparse import ArgumentParser
from operator import attrgetter
from typing import Dict, List

from binary_reader import BinaryReader


class Segment:
    loadable: bool
    readable: bool

    file_addr: int
    virt_addr: int
    file_size: int
    mem_size: int
    align: int


ALIGN_SIZE = 0x10000
EXAMPLE_JSON = """
{
    "strings": [
        {
            "text": "Test",
            "address": "0xC54E10"
        },
        {
            "text": "Test 2",
            "address": 12930592
        }
    ]
}
"""

segments: List[Segment] = list()
program_start = 0


def find_pointer(buf: bytearray, addr: int, safe: bool) -> int:
    result = -1

    for seg in segments:
        if addr > seg.file_addr and addr < seg.file_addr + seg.file_size:
            addr = addr - seg.file_addr + seg.virt_addr
            result = buf.find(addr.to_bytes(4, 'big'), program_start)

            if safe and buf.find(addr.to_bytes(4, 'big'), result + 4) != -1:
                # error code for "multiple instances found"
                return -2

    return result


def patch_eboot(eboot, patched_eboot, json_file, verbose, update, safe, align_value, encoding):
    global segments
    global program_start

    with open(eboot, 'rb') as f:
        elf = BinaryReader(f.read(), big_endian=True, encoding=encoding)

    print(f'Reading {json_file}...')
    with open(json_file, 'r', encoding=encoding) as j:
        strings = json.load(j)
        strings: List[Dict] = strings.get("strings", None)

        if not strings:
            raise Exception('Invalid JSON. Could not find "strings" element.')

    print(f'Reading {eboot}...')
    with elf.seek_to(1):
        if elf.read_str(3) != 'ELF':
            raise Exception('Invalid magic. Make sure the EBOOT was decrypted into an ELF.')

    with elf.seek_to(5):
        if elf.read_int8() != 2:
            raise Exception('Unexpected endianness. Expected big endian.')

    # program header offset
    with elf.seek_to(0x20):
        program_header = elf.read_uint64()

    with elf.seek_to(0x36):
        program_header_entry_size = elf.read_uint16()
        program_header_entry_count = elf.read_uint16()

        if (program_header_entry_size != 0x38):
            raise Exception('Unknown program header entry size. Cannot modify segment data.')

    # read program segments
    segments = list()
    with elf.seek_to(program_header):
        for i in range(program_header_entry_count):
            seg = Segment()

            elf.seek(3, whence=1)
            seg.loadable = elf.read_uint8() == 1
            elf.seek(3, whence=1)
            seg.readable = elf.read_uint8() & 4 != 0

            seg.file_addr = elf.read_uint64()
            seg.virt_addr = elf.read_uint64()

            if elf.read_uint64() != seg.virt_addr:
                print(f'Warning: skipped segment {i} because physical address did not match virtual address.')
                elf.seek(8 * 3, whence=1)
                continue

            seg.file_size = elf.read_uint64()
            seg.mem_size = elf.read_uint64()

            seg.align = elf.read_uint64()

            segments.append(seg)

        program_start = elf.pos()

    # section header offset
    with elf.seek_to(0x28):
        was_trimmed = True
        section_header = elf.read_uint64()

        if section_header == 0:
            was_trimmed = False
        else:
            elf.seek(0x3A)  # section header entry size

            # check if one of the 3 values is not 0
            if any(elf.read_uint16(3)):
                print('Trimming section table...')

                was_trimmed = False
                elf.trim(section_header)
                elf.align(ALIGN_SIZE)

                # clear section header info
                with elf.seek_to(0x3A):
                    elf.write_uint16([0] * 3, is_iterable=True)

    empty_seg_index = -1
    if was_trimmed:
        for i in range(len(segments)):
            if segments[i].file_addr == section_header:
                empty_seg_index = i

    if empty_seg_index == -1:
        print('Looking for a suitable empty segment...')
        for i in range(len(segments)):
            if segments[i].loadable and \
                    segments[i].readable and \
                    segments[i].virt_addr == \
                    segments[i].file_size == \
                    segments[i].mem_size == 0:

                empty_seg_index = i
                break

        if empty_seg_index == -1:
            raise Exception('Could not find an empty segment.')

        index_of_max = segments.index(
            max(segments, key=attrgetter('virt_addr')))

        align = int(align_value) if align_value else ALIGN_SIZE

        # set virtual address of empty segment to start after the end of the last (virtual) segment
        virtual_address = segments[index_of_max].virt_addr + segments[index_of_max].mem_size
        virtual_address += align - (virtual_address % align)

        segments[empty_seg_index].virt_addr = virtual_address
        segments[empty_seg_index].file_addr = elf.size()

    print(f'Found empty segment: {empty_seg_index}\n')

    segment = segments[empty_seg_index]

    if update:
        # start from the end of the previous run
        elf.seek(segment.file_addr + segment.file_size)
    else:
        elf.seek(segment.file_addr)

    # store a copy of the buffer for searching
    buffer = elf.buffer()

    print('Patching strings...')
    for i in range(len(strings)):
        # a small portion of the string for printing
        print_string = f'"{strings[i]["text"][:20]}..."'

        if (not (strings[i].get('text', None) or strings[i].get('address', None))):
            print(f'Warning: skipped string {i} because the JSON object is invalid: {print_string}')
            continue

        if (update):
            old_address = buffer.find(strings[i]['text'].encode(encoding), segment.file_addr)
            if old_address != -1:
                if (verbose):
                    print(f'Skipped string {i} because it was previously added: {print_string}')
                continue

        if type(strings[i]['address']) is str:
            address = int(strings[i]['address'], 0)
        else:
            address = strings[i]['address']

        # find the pointer to the old string's address
        pointer = find_pointer(buffer, address, safe)

        if pointer == -1:
            print(f'Warning: skipped string {i} because its address ({address}) was not found: {print_string}')
            continue
        elif pointer == -2:
            # only returned if safe is true
            print(f'Warning: skipped string {i} because its address was found multiple times: {print_string}')
            continue

        current_pos = elf.pos()
        with elf.seek_to(pointer):
            elf.write_uint32(
                current_pos - segment.file_addr + segment.virt_addr)

        elf.write_str(strings[i]['text'], null=True)
        elf.align(8)

        if verbose:
            print(f'Patched string {i} at {address}: {print_string}')

    # write the elf
    with elf.seek_to(0x28):
        elf.write_uint64(segment.file_addr)

    segment.file_size = elf.size() - segment.file_addr
    segment.mem_size = segment.file_size

    with elf.seek_to(program_header + (program_header_entry_size * empty_seg_index)):
        elf.seek(8, whence=1)
        elf.write_uint64(segment.file_addr)
        elf.write_uint64(segment.virt_addr)
        elf.write_uint64(segment.virt_addr)
        elf.write_uint64(segment.file_size)
        elf.write_uint64(segment.mem_size)

    elf.align(segment.align)

    with open(patched_eboot, 'wb') as f:
        f.write(elf.buffer())
        print(f'\nWrote to {patched_eboot}')


def main():
    print(f'Eboot String Patcher v{__version__}')
    print(f'By {__author__}\n')

    parser = ArgumentParser(
        description="""Replaces strings without size limits by patching pointers in PS3 EBOOT""")
    parser.add_argument('json', nargs='?', action='store', default='eboot.json',
                        help='path to JSON file with the new strings (use --json-help for the format info)')
    parser.add_argument('input', nargs='?', action='store', default='EBOOT.ELF',
                        help='path to input EBOOT.ELF')
    parser.add_argument('output', nargs='?', action='store', default=None,
                        help='path to output EBOOT.ELF')
    parser.add_argument('-j', '--json-help', dest='json_help', action='store_true',
                        help='show help info about the JSON file format and exit')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='show info about each string entry that is patched')
    parser.add_argument('-u', '--update', action='store_true',
                        help='skip adding strings that were added in a previous run (does not check for conflicts)')
    parser.add_argument('-s', '--safe', action='store_true',
                        help='skip strings if their address was found multiple times (use this whenever the script breaks the eboot)')
    parser.add_argument('-a', '--align-value', dest='align_value', action='store', required=False,
                        help='force alignment of the segment before the empty segment to the value given')
    parser.add_argument('-e', '--encoding', action='store', default='cp936',
                        help='set the encoding when reading the json and strings in the eboot (default is cp963; for Japanese text)')

    args = parser.parse_args()

    if args.json_help:
        print('Showing JSON format help:\n')
        print('The JSON file should have only 1 object called "strings", which contains an array of objects,')
        print('each with 2 elements: "text" and "address". "text" is the new string that will replace the old string at "address".')
        print('"address" must be a valid file offset in the input eboot, and can be either written in hex (as a string) or in decimal.')
        print('\nIMPORTANT: if an entry is removed from the JSON after running the script once, a clean EBOOT should be used.')
        print('Otherwise, running the script multiple times on the same EBOOT should not have any side effects.')
        print('\nHere\'s an example:')
        print(EXAMPLE_JSON)
        os.system('pause')
        return

    eboot = args.input
    patched_eboot = args.output
    json_file = args.json

    if not patched_eboot:
        patched_eboot = eboot.rsplit('.', 1)[0] + '_PATCHED.ELF'

    if (not os.path.isfile(json_file)):
        print('Error: input JSON file does not exist. Aborting.')
        os.system('pause')
        return

    if (not os.path.isfile(eboot)):
        print('Error: input EBOOT file does not exist. Aborting.')
        os.system('pause')
        return

    if (os.path.exists(patched_eboot)):
        if input('Output file already exists. Overwrite? (y/n): ').lower() != 'y':
            print('Aborting.')
            os.system('pause')
            return

        print()

    patch_eboot(eboot, patched_eboot, json_file, args.verbose, args.update, args.safe, args.align_value, args.encoding)

    print('Finished.')


if __name__ == '__main__':
    main()