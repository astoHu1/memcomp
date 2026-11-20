/* SPDX-License-Identifier: GPL-2.0 */
#ifndef MEMCOMP_MACOS_HOST_H
#define MEMCOMP_MACOS_HOST_H
/* Darwin exposes uuid_t through several independent system headers. */
#define uuid_t darwin_uuid_t
#include <sys/_types/_uuid_t.h>
#include <sys/types.h>
#include <stdlib.h>
#include <unistd.h>
#undef uuid_t
#endif
