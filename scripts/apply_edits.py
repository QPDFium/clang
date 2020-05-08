#!/usr/bin/env python
# Copyright (c) 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Applies edits generated by a clang tool that was run on Chromium code.

Synopsis:

  cat run_tool.out | extract_edits.py | apply_edits.py <build dir> <filters...>

For example - to apply edits only to WTF sources:

  ... | apply_edits.py out/gn third_party/WebKit/Source/wtf

In addition to filters specified on the command line, the tool also skips edits
that apply to files that are not covered by git.
"""

import argparse
import collections
import functools
import multiprocessing
import os
import os.path
import re
import subprocess
import sys

script_dir = os.path.dirname(os.path.realpath(__file__))
tool_dir = os.path.abspath(os.path.join(script_dir, '../pylib'))
sys.path.insert(0, tool_dir)

from clang import compile_db

Edit = collections.namedtuple('Edit',
                              ('edit_type', 'offset', 'length', 'replacement'))


def _GetFilesFromGit(paths=None):
  """Gets the list of files in the git repository.

  Args:
    paths: Prefix filter for the returned paths. May contain multiple entries.
  """
  args = []
  if sys.platform == 'win32':
    args.append('git.bat')
  else:
    args.append('git')
  args.append('ls-files')
  if paths:
    args.extend(paths)
  command = subprocess.Popen(args, stdout=subprocess.PIPE)
  output, _ = command.communicate()
  return [os.path.realpath(p) for p in output.splitlines()]


def _ParseEditsFromStdin(build_directory):
  """Extracts generated list of edits from the tool's stdout.

  The expected format is documented at the top of this file.

  Args:
    build_directory: Directory that contains the compile database. Used to
      normalize the filenames.
    stdout: The stdout from running the clang tool.

  Returns:
    A dictionary mapping filenames to the associated edits.
  """
  path_to_resolved_path = {}
  def _ResolvePath(path):
    if path in path_to_resolved_path:
      return path_to_resolved_path[path]

    if not os.path.isfile(path):
      resolved_path = os.path.realpath(os.path.join(build_directory, path))
    else:
      resolved_path = path

    if not os.path.isfile(resolved_path):
      sys.stderr.write('Edit applies to a non-existent file: %s\n' % path)
      resolved_path = None

    path_to_resolved_path[path] = resolved_path
    return resolved_path

  edits = collections.defaultdict(list)
  for line in sys.stdin:
    line = line.rstrip("\n\r")
    try:
      edit_type, path, offset, length, replacement = line.split(':::', 4)
      replacement = replacement.replace('\0', '\n')
      path = _ResolvePath(path)
      if not path: continue
      edits[path].append(Edit(edit_type, int(offset), int(length), replacement))
    except ValueError:
      sys.stderr.write('Unable to parse edit: %s\n' % line)
  return edits


_PLATFORM_SUFFIX = \
    r'(?:_(?:android|aura|chromeos|ios|linux|mac|ozone|posix|win|x11))?'
_TEST_SUFFIX = \
    r'(?:_(?:browser|interactive_ui|ui|unit)?test)?'
_suffix_regex = re.compile(_PLATFORM_SUFFIX + _TEST_SUFFIX)


def _FindPrimaryHeaderBasename(filepath):
  """ Translates bar/foo.cc -> foo
                 bar/foo_posix.cc -> foo
                 bar/foo_unittest.cc -> foo
                 bar/foo.h -> None
  """
  dirname, filename = os.path.split(filepath)
  basename, extension = os.path.splitext(filename)
  if extension == '.h':
    return None
  basename = _suffix_regex.sub('', basename)
  return basename


_INCLUDE_INSERTION_POINT_REGEX_TEMPLATE = r'''
   ^(?!               # Match the start of the first line that is
                      # not one of the following:

      \s+             # 1. Line starting with whitespace
                      #    (this includes blank lines and continuations of
                      #     C comments that start with whitespace/indentation)

    | //              # 2a. A C++ comment
    | /\*             # 2b. A C comment
    | \*              # 2c. A continuation of a C comment
                      #     (see also rule 1. above)

    | \xef \xbb \xbf  # 3. "Lines" starting with BOM character

      # 4. Include guards (Chromium-style)
    | \#ifndef \s+ [A-Z0-9_]+_H ( | _ | __ ) \b \s* $
    | \#define \s+ [A-Z0-9_]+_H ( | _ | __ ) \b \s* $

      # 4b. Include guards (anything that repeats):
      #     - the same <guard> has to repeat in both the #ifndef and the #define
      #     - #define has to be "simple" - either:
      #         - either: #define GUARD
      #         - or    : #define GUARD 1
    | \#ifndef \s+ (?P<guard> [A-Za-z0-9_]* ) \s* $ ( \n | \r )* ^
      \#define \s+ (?P=guard) \s* ( | 1 \s* ) $
    | \#define \s+ (?P=guard) \s* ( | 1 \s* ) $  # Skipping previous line.

      # 5. A C/C++ system include
    | \#include \s* < .* >

      # 6. A primary header include
      #    (%%s should be the basename returned by _FindPrimaryHeaderBasename).
      #
      # TODO(lukasza): Do not allow any directory below - require the top-level
      # directory to be the same and at least one itermediate dirname to be the
      # same.
    | \#include \s*   "
          [^"]* \b       # Allowing any directory
          %s[^"/]*\.h "  # Matching both basename.h and basename_posix.h
    )
'''


def _InsertNonSystemIncludeHeader(filepath, header_line_to_add, contents):
  """ Mutates |contents| (contents of |filepath|) to #include
      the |header_to_add
  """
  # Don't add the header if it is already present.
  replacement_text = header_line_to_add
  if replacement_text in contents:
    return
  replacement_text += '\n'

  # Find the right insertion point.
  #
  # Note that we depend on a follow-up |git cl format| for the right order of
  # headers.  Therefore we just need to find the right header group (e.g. skip
  # system headers and the primary header).
  primary_header_basename = _FindPrimaryHeaderBasename(filepath)
  if primary_header_basename is None:
    primary_header_basename = ':this:should:never:match:'
  regex_text = _INCLUDE_INSERTION_POINT_REGEX_TEMPLATE % primary_header_basename
  match = re.search(regex_text, contents, re.MULTILINE | re.VERBOSE)
  assert (match is not None)
  insertion_point = match.start()

  # Extra empty line is required if the addition is not adjacent to other
  # includes.
  if not contents[insertion_point:].startswith('#include'):
    replacement_text += '\n'

  # Make the edit.
  contents[insertion_point:insertion_point] = replacement_text


def _ApplyReplacement(filepath, contents, edit, last_edit):
  if (last_edit is not None and edit.edit_type == last_edit.edit_type
      and edit.offset == last_edit.offset and edit.length == last_edit.length):
    raise ValueError(('Conflicting replacement text: ' +
                      '%s at offset %d, length %d: "%s" != "%s"\n') %
                     (filepath, edit.offset, edit.length, edit.replacement,
                      last_edit.replacement))

  contents[edit.offset:edit.offset + edit.length] = edit.replacement
  if not edit.replacement:
    _ExtendDeletionIfElementIsInList(contents, edit.offset)


def _ApplyIncludeHeader(filepath, contents, edit, last_edit):
  header_line_to_add = '#include "%s"' % edit.replacement
  _InsertNonSystemIncludeHeader(filepath, header_line_to_add, contents)


def _ApplySingleEdit(filepath, contents, edit, last_edit):
  if edit.edit_type == 'r':
    _ApplyReplacement(filepath, contents, edit, last_edit)
  elif edit.edit_type == 'include-user-header':
    _ApplyIncludeHeader(filepath, contents, edit, last_edit)
  else:
    raise ValueError('Unrecognized edit directive "%s": %s\n' %
                     (edit.edit_type, filepath))


def _ApplyEditsToSingleFileContents(filepath, contents, edits):
  # Sort the edits and iterate through them in reverse order. Sorting allows
  # duplicate edits to be quickly skipped, while reversing means that
  # subsequent edits don't need to have their offsets updated with each edit
  # applied.
  #
  # Note that after sorting in reverse, the 'i' directives will come after 'r'
  # directives.
  edits.sort(reverse=True)

  edit_count = 0
  error_count = 0
  last_edit = None
  for edit in edits:
    if edit == last_edit:
      continue
    try:
      _ApplySingleEdit(filepath, contents, edit, last_edit)
      last_edit = edit
      edit_count += 1
    except ValueError as err:
      sys.stderr.write(str(err) + '\n')
      error_count += 1

  return (edit_count, error_count)


def _ApplyEditsToSingleFile(filepath, edits):
  with open(filepath, 'rb+') as f:
    contents = bytearray(f.read())
    edit_and_error_counts = _ApplyEditsToSingleFileContents(
        filepath, contents, edits)
    f.seek(0)
    f.truncate()
    f.write(contents)
  return edit_and_error_counts


def _ApplyEdits(edits):
  """Apply the generated edits.

  Args:
    edits: A dict mapping filenames to Edit instances that apply to that file.
  """
  edit_count = 0
  error_count = 0
  done_files = 0
  for k, v in edits.iteritems():
    tmp_edit_count, tmp_error_count = _ApplyEditsToSingleFile(k, v)
    edit_count += tmp_edit_count
    error_count += tmp_error_count
    done_files += 1
    percentage = (float(done_files) / len(edits)) * 100
    sys.stdout.write('Applied %d edits (%d errors) to %d files [%.2f%%]\r' %
                     (edit_count, error_count, done_files, percentage))

  sys.stdout.write('\n')
  return -error_count


_WHITESPACE_BYTES = frozenset((ord('\t'), ord('\n'), ord('\r'), ord(' ')))


def _ExtendDeletionIfElementIsInList(contents, offset):
  """Extends the range of a deletion if the deleted element was part of a list.

  This rewriter helper makes it easy for refactoring tools to remove elements
  from a list. Even if a matcher callback knows that it is removing an element
  from a list, it may not have enough information to accurately remove the list
  element; for example, another matcher callback may end up removing an adjacent
  list element, or all the list elements may end up being removed.

  With this helper, refactoring tools can simply remove the list element and not
  worry about having to include the comma in the replacement.

  Args:
    contents: A bytearray with the deletion already applied.
    offset: The offset in the bytearray where the deleted range used to be.
  """
  char_before = char_after = None
  left_trim_count = 0
  for byte in reversed(contents[:offset]):
    left_trim_count += 1
    if byte in _WHITESPACE_BYTES:
      continue
    if byte in (ord(','), ord(':'), ord('('), ord('{')):
      char_before = chr(byte)
    break

  right_trim_count = 0
  for byte in contents[offset:]:
    right_trim_count += 1
    if byte in _WHITESPACE_BYTES:
      continue
    if byte == ord(','):
      char_after = chr(byte)
    break

  if char_before:
    if char_after:
      del contents[offset:offset + right_trim_count]
    elif char_before in (',', ':'):
      del contents[offset - left_trim_count:offset]


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument(
      '-p',
      required=True,
      help='path to the build dir (dir that edit paths are relative to)')
  parser.add_argument(
      'path_filter',
      nargs='*',
      help='optional paths to filter what files the tool is run on')
  args = parser.parse_args()

  filenames = set(_GetFilesFromGit(args.path_filter))
  edits = _ParseEditsFromStdin(args.p)
  return _ApplyEdits(
      {k: v for k, v in edits.iteritems()
            if os.path.realpath(k) in filenames})


if __name__ == '__main__':
  sys.exit(main())
