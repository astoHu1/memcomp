CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Wpedantic -std=c11
CPPFLAGS ?=
LDFLAGS ?= -static

TARGET := ksm_memcomp_test

.PHONY: all clean

all: $(TARGET)

$(TARGET): ksm_memcomp_test.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $<

clean:
	rm -f $(TARGET)
