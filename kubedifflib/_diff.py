from __future__ import print_function
from __future__ import division
from builtins import str
from past.builtins import basestring
from builtins import object
from future.utils import listitems, viewitems
from fnmatch import fnmatchcase
from functools import partial
import collections
import difflib
import json
import numbers
import operator
import os
import subprocess
import sys
import yaml
import re

from ._kube import (
  KubeObject,
  iter_files,
)


class Difference(object):
  """An observed difference."""

  def __init__(self, message, path, *args):
    self.message = message
    self.path = path
    self.args = args

  def to_text(self, kind=''):
    if 'secret' in kind.lower() and len(self.args) == 2:
      message = self.message % ((len(self.args[0]) * '*'), (len(self.args[1]) * '*'))

    else:
      message = self.message % self.args
    if self.path is None:
      return message
    return '%s: %s' % (self.path, message)


def cpus_equal(a, b):
  """Compares cpu specs for equality. cpu specs can be ints, floats or strings '{:d}m',
  where 'm' denotes 'milli-', ie. 1/1000th of an integer value.
  Eg. "50m" is equal to 0.05."""
  parse = lambda x: int(x[:-1])/1000. if x.endswith('m') else float(x)
  return parse(a) == parse(b)


# Tolerations specify a path (glob patterns accepted) and a check function.
# The check function takes (want, have) and is called on matching paths.
# If it returns True then any difference is 'tolerated', ie. ignored.
tolerations = {
  "*.resources.requests.cpu": cpus_equal,
  "*.resources.limits.cpu": cpus_equal,
}


def different_lengths(path, want, have):
  return Difference("Unequal lengths: %d != %d", path, len(want), len(have))

missing_item = partial(Difference, "'%s' missing")
not_equal = partial(Difference, "'%s' != '%s'")
missing_item_in_running_configuration = partial(Difference, "'%s' missing in running configuration")
missing_item_in_wished_configuration = partial(Difference, "'%s' missing in wished configuration")


def diff_not_equal(path, want, have):
  want_lines, have_lines = want.splitlines(), have.splitlines()
  diff = "\n".join(difflib.unified_diff(want_lines, have_lines, fromfile=path, tofile="running", lineterm=""))
  return Difference("Diff:\n%s", path, diff)


def diff_lists(path, want, have):
  if not len(want) == len(have):
    yield different_lengths(path, want, have)

    if re.match('^\.spec\.template\.spec\.containers\[\d+\]\.env$', path):
      in_want = set(map(lambda x: x['name'], want))
      in_have = set(map(lambda x: x['name'], have))
      in_both = in_want & in_have
      in_want_only = in_want - in_both
      in_have_only = in_have - in_both

      for i in in_want_only:
        yield Difference("Diff: {} in wished config only".format(i), path)

      for i in in_have_only:
        yield Difference("Diff: {} in running config only".format(i), path)

      want_in_both = filter(lambda x: x['name'] in in_both, want)
      have_in_both = filter(lambda x: x['name'] in in_both, have)

      for i, (want_v, have_v) in enumerate(zip(want_in_both, have_in_both)):
        for difference in diff("%s[%d]" % (path, i), want_v, have_v):
          yield difference

  else:
    for i, (want_v, have_v) in enumerate(zip(want, have)):
      for difference in diff("%s[%d]" % (path, i), want_v, have_v):
        yield difference


def diff_dicts(path, want, have):
  if path == ".metadata.labels":
    for (k, have_v) in viewitems(have):
      key_path = "%s.%s" % (path, k)

      if k not in want:
        yield missing_item_in_wished_configuration(path, k)

  for (k, want_v) in viewitems(want):
    key_path = "%s.%s" % (path, k)

    if k not in have:
      yield missing_item_in_running_configuration(path, k)
    else:
      for difference in diff(key_path, want_v, have[k]):
        yield difference


def normalize(value):
  if isinstance(value, numbers.Number):
    return str(value)
  if value == [] or value == {}:
    return None
  return value


def diff(path, want, have):
  want = normalize(want)
  have = normalize(have)

  for (toleration_path, toleration_check) in listitems(tolerations):
    if fnmatchcase(path, toleration_path) and toleration_check(want, have):
      return

  if isinstance(want, dict) and isinstance(have, dict):
    for difference in diff_dicts(path, want, have):
      yield difference

  elif isinstance(want, list) and isinstance(have, list):
    for difference in diff_lists(path, want, have):
      yield difference

  elif isinstance(want, basestring) and isinstance(have, basestring):
    if want != have:
      if "\n" in want:
        yield diff_not_equal(path, want, have)
      else:
        yield not_equal(path, want, have)

  elif want != have:
    yield not_equal(path, want, have)


def check_file(printer, path, config):
  """Check YAML file 'path' for differences.

  :param printer: Where we report differences to.
  :param str path: The YAML file to test.
  :param dict config: Contains Kubernetes parsing and access configuration.
  :return: Number of differences found.
  """
  with open(path, 'r') as stream:
    expected = yaml.safe_load_all(stream)

    differences = 0
    for data in expected:
      # data can be None, e.g. in cases where the doc ends with a '---'
      if not data:
        continue
      try:
        for kube_obj in KubeObject.from_dict(data, config["namespace"]):
          printer.add(path, kube_obj)

          try:
            running = kube_obj.get_from_cluster(config["kubeconfig"])
          except subprocess.CalledProcessError as e:
            printer.diff(path, Difference(e.output, None))
            differences += 1
            continue

          for difference in diff("", kube_obj.data, running):
            differences += 1
            printer.diff(path, difference)
      except Exception:
        print("Failed parsing %s." % (path))
        raise

    return differences


class StdoutPrinter(object):
  def add(self, _, kube_obj):
    print("Checking %s '%s'" % (kube_obj.kind, kube_obj.namespaced_name))

  def diff(self, _, difference):
    print(" *** " + difference.to_text())

  def finish(self):
    pass


class QuietTextPrinter(object):
  """Only output if there's a difference."""

  def __init__(self, stream=None):
    self._stream = stream if stream else sys.stdout
    self._current = None

  def _write(self, msg, *args):
    self._stream.write(msg % args)
    self._stream.write('\n')
    self._stream.flush()

  def add(self, _, kube_obj):
    self._current = kube_obj

  def diff(self, _, difference):
    if self._current:
      self._write('## %s (%s)', self._current.namespaced_name, self._current.kind)
    else:
      self._write('## UNKNOWN')
    self._write('')
    self._write('%s', difference.to_text(self._current.kind))

  def finish(self):
    pass


class JSONPrinter(object):
  def __init__(self):
    self.data = collections.defaultdict(list)

  def add(self, path, kube_obj):
    pass

  def diff(self, path, difference):
    self.data[path].append(difference.to_text())

  def finish(self):
    print(json.dumps(self.data, sort_keys=True, indent=2, separators=(',', ': ')))


def check_files(paths, printer, config):
  """Check all files in 'paths' for differences to a Kubernetes cluster.

  :param printer: Where differences are reported to as they are found.
  :param dict config: Contains Kubernetes parsing and access configuration.
  :return: True if there are differences, False otherwise.
  """
  differences = 0
  for path in iter_files(paths):
    _, extension = os.path.splitext(path)
    if extension not in [".yaml", ".yml"]:
      continue
    differences += check_file(printer, path, config)

  printer.finish()
  return bool(differences)
