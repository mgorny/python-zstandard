# Copyright (c) 2016-present, Gregory Szorc
# All rights reserved.
#
# This software may be modified and distributed under the terms
# of the BSD license. See the LICENSE file for details.

from __future__ import absolute_import

import distutils.ccompiler
import distutils.sysconfig
import os
import re
import subprocess
import tempfile

import cffi

# cffi can't parse some of the primitives in zstd.h. So we invoke the
# preprocessor and feed its output into cffi.
compiler = distutils.ccompiler.new_compiler()

# Needed for MSVC.
if hasattr(compiler, "initialize"):
    compiler.initialize()

# This performs platform specific customizations, including honoring
# environment variables like CC.
distutils.sysconfig.customize_compiler(compiler)

# Distutils doesn't always set compiler.preprocessor, so invoke the
# preprocessor manually when needed.
args = getattr(compiler, "preprocessor", None)
if compiler.compiler_type == "unix":
    if not args:
        # Using .compiler respects the CC environment variable.
        args = [compiler.compiler[0], "-E"]
    args.extend(
        [
            "-DZSTD_STATIC_LINKING_ONLY",
            "-DZDICT_STATIC_LINKING_ONLY",
        ]
    )
elif compiler.compiler_type == "msvc":
    if not args:
        args = [compiler.cc, "/EP"]
    args.extend(
        [
            "/DZSTD_STATIC_LINKING_ONLY",
            "/DZDICT_STATIC_LINKING_ONLY",
        ]
    )
else:
    raise Exception("unsupported compiler type: %s" % compiler.compiler_type)


def preprocess(path):
    with open(path, "rb") as fh:
        lines = []
        it = iter(fh)

        for line in it:
            # zstd.h includes <stddef.h>, which is also included by cffi's
            # boilerplate. This can lead to duplicate declarations. So we strip
            # this include from the preprocessor invocation.
            #
            # The same things happens for including zstd.h, so give it the same
            # treatment.
            #
            # We define ZSTD_STATIC_LINKING_ONLY, which is redundant with the inline
            # #define in zstdmt_compress.h and results in a compiler warning. So drop
            # the inline #define.
            if line.startswith(
                (
                    b"#include <stddef.h>",
                    b'#include "zstd.h"',
                    b"#define ZSTD_STATIC_LINKING_ONLY",
                )
            ):
                continue

            # The preprocessor environment on Windows doesn't define include
            # paths, so the #include of limits.h fails. We work around this
            # by removing that import and defining INT_MAX ourselves. This is
            # a bit hacky. But it gets the job done.
            # TODO make limits.h work on Windows so we ensure INT_MAX is
            # correct.
            if line.startswith(b"#include <limits.h>"):
                line = b"#define INT_MAX 2147483647\n"

            # ZSTDLIB_API may not be defined if we dropped zstd.h. It isn't
            # important so just filter it out. Ditto for ZSTDLIB_STATIC_API and
            # ZDICTLIB_STATIC_API.
            for prefix in (
                b"ZSTDLIB_API",
                b"ZSTDLIB_STATIC_API",
                b"ZDICTLIB_STATIC_API",
            ):
                if line.startswith(prefix):
                    line = line[len(prefix) :]

            lines.append(line)

    fd, input_file = tempfile.mkstemp(suffix=".h")
    os.write(fd, b"".join(lines))
    os.close(fd)

    try:
        env = dict(os.environ)
        # cffi attempts to decode source as ascii. And the preprocessor
        # may insert non-ascii for some annotations. So try to force
        # ascii output via LC_ALL.
        env["LC_ALL"] = "C"

        if getattr(compiler, "_paths", None):
            env["PATH"] = compiler._paths
        process = subprocess.Popen(
            args + [input_file], stdout=subprocess.PIPE, env=env
        )
        output = process.communicate()[0]
        ret = process.poll()
        if ret:
            raise Exception("preprocessor exited with error")

        return output
    finally:
        os.unlink(input_file)


def normalize_output(output):
    lines = []
    for line in output.splitlines():
        # CFFI's parser doesn't like __attribute__ on UNIX compilers.
        if line.startswith(b'__attribute__ ((visibility ("default"))) '):
            line = line[len(b'__attribute__ ((visibility ("default"))) ') :]

        if line.startswith(b"__attribute__((deprecated"):
            continue
        elif b"__declspec(deprecated(" in line:
            continue
        elif line.startswith(b"__attribute__((__unused__))"):
            continue

        lines.append(line)

    return b"\n".join(lines)


def get_ffi(system_zstd = False):
    zstd_sources = []
    include_dirs = []
    libraries = []

    if not system_zstd:
        here = os.path.abspath(os.path.dirname(__file__))

        zstd_sources += [
            "zstd/zstd.c",
        ]

        # Headers whose preprocessed output will be fed into cdef().
        headers = [os.path.join(here, "zstd", p) for p in ("zstd.h", "zdict.h")]

        include_dirs += [
            os.path.join(here, "zstd"),
        ]
    else:
        libraries += ["zstd"]

        # Locate headers using the preprocessor.
        include_re = re.compile(r'^# \d+ "([^"]+/(?:zstd|zdict)\.h)"')
        with tempfile.TemporaryDirectory() as temp_dir:
            with open(os.path.join(temp_dir, "input.h"), "w") as f:
                f.write("#include <zstd.h>\n#include <zdict.h>\n")
            compiler.preprocess(os.path.join(temp_dir, "input.h"),
                                os.path.join(temp_dir, "output.h"))
            with open(os.path.join(temp_dir, "output.h"), "r") as f:
                headers = list({
                    m.group(1) for m in map(include_re.match, f)
                    if m is not None
                })

    ffi = cffi.FFI()
    # zstd.h uses a possible undefined MIN(). Define it until
    # https://github.com/facebook/zstd/issues/976 is fixed.
    # *_DISABLE_DEPRECATE_WARNINGS prevents the compiler from emitting a warning
    # when cffi uses the function. Since we statically link against zstd, even
    # if we use the deprecated functions it shouldn't be a huge problem.
    ffi.set_source(
        "zstandard._cffi",
        """
    #define MIN(a,b) ((a)<(b) ? (a) : (b))
    #define ZSTD_STATIC_LINKING_ONLY
    #define ZSTD_DISABLE_DEPRECATE_WARNINGS
    #include <zstd.h>
    #define ZDICT_STATIC_LINKING_ONLY
    #define ZDICT_DISABLE_DEPRECATE_WARNINGS
    #include <zdict.h>
    """,
        sources=zstd_sources,
        include_dirs=include_dirs,
        libraries=libraries,
    )

    DEFINE = re.compile(b"^\\#define ([a-zA-Z0-9_]+) ")

    sources = []

    # Feed normalized preprocessor output for headers into the cdef parser.
    for header in headers:
        preprocessed = preprocess(header)
        sources.append(normalize_output(preprocessed))

        # #define's are effectively erased as part of going through preprocessor.
        # So perform a manual pass to re-add those to the cdef source.
        with open(header, "rb") as fh:
            for line in fh:
                line = line.strip()
                m = DEFINE.match(line)
                if not m:
                    continue

                if m.group(1) == b"ZSTD_STATIC_LINKING_ONLY":
                    continue

                # The parser doesn't like some constants with complex values.
                if m.group(1) in (b"ZSTD_LIB_VERSION", b"ZSTD_VERSION_STRING"):
                    continue

                # The ... is magic syntax by the cdef parser to resolve the
                # value at compile time.
                sources.append(m.group(0) + b" ...")

    cdeflines = b"\n".join(sources).splitlines()
    cdeflines = [line for line in cdeflines if line.strip()]
    ffi.cdef(b"\n".join(cdeflines).decode("latin1"))
    return ffi


if __name__ == "__main__":
    get_ffi().compile()
