# SPDX-License-Identifier: GPL-2.0-only
CC ?= cc
CFLAGS ?= -O2 -g
CFLAGS += -std=gnu11 -Wall -Wextra -Wformat=2 -Wshadow
O ?= .
OUT := $(abspath $(O))
TOOLS := refault shmem app_switch guest

.PHONY: all clean
all: $(addprefix $(OUT)/,$(TOOLS))

$(OUT)/refault: refault.c common.h corpus.h
	@mkdir -p $(@D)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $< $(LDLIBS)

$(OUT)/shmem: shmem.c common.h corpus.h
	@mkdir -p $(@D)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $< $(LDLIBS)

$(OUT)/app_switch: app_switch.c corpus.h
	@mkdir -p $(@D)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $< $(LDLIBS)

$(OUT)/guest: guest.c
	@mkdir -p $(@D)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $< $(LDLIBS)

clean:
	$(RM) -- $(addprefix $(OUT)/,$(TOOLS))
