# Mozart Preprocessing Pipeline — Makefile
# ============================================================================
# Targets:
#   make [all]       Build library + example (Release, RNNoise linked)
#   make debug        Build with debug symbols + assertions
#   make stub         Build without RNNoise (passthrough stub)
#   make clean        Remove build artifacts
#   make run          Build and run the smoke-test example
#   make help         Show this help
# ============================================================================

CC       := gcc
AR       := ar
RM       := rm -rf
MKDIR    := mkdir -p

# ---- Directories -------------------------------------------------------------
SRC_DIR      := src
INC_DIR      := include
RNNOISE_DIR  := native/rnnoise
RNNOISE_SRC  := $(RNNOISE_DIR)/src
RNNOISE_INC  := $(RNNOISE_DIR)/include
BUILD_DIR    := build
OBJ_DIR      := $(BUILD_DIR)/obj
LIB_DIR      := $(BUILD_DIR)/lib
BIN_DIR      := $(BUILD_DIR)/bin

# ---- Target artifacts --------------------------------------------------------
LIB_RNNOISE    := $(LIB_DIR)/librnnoise.a
LIB_MOZART     := $(LIB_DIR)/libmozart_pre.a
BIN_EXAMPLE    := $(BIN_DIR)/mozart_pre_example

# ---- Flags: ARM NEON + DOTPROD + FP16 (Jetson Orin Cortex-A78AE) ------------
ARM_ARCH_FLAGS := -march=armv8.2-a+fp16+dotprod -mtune=cortex-a78ae
OPT_FLAGS      := -O3 -ffast-math -funroll-loops -flto
WARN_FLAGS     := -Wall -Wextra -Wpedantic
INC_FLAGS      := -I$(INC_DIR) -I. -I$(RNNOISE_INC) -I$(RNNOISE_SRC)

CFLAGS_BASE    := -std=c11 $(WARN_FLAGS) $(INC_FLAGS)
CFLAGS_RELEASE := $(CFLAGS_BASE) -DNDEBUG $(ARM_ARCH_FLAGS) $(OPT_FLAGS)
CFLAGS_DEBUG   := $(CFLAGS_BASE) -g -O0 -DDEBUG
CFLAGS_STUB    := $(CFLAGS_BASE) -DNDEBUG $(ARM_ARCH_FLAGS) $(OPT_FLAGS)

LDFLAGS        := -lm -lrt

# ---- RNNoise source files (no x86 intrinsics, ARM NEON path only) ------------
RNNOISE_SRCS := $(RNNOISE_SRC)/denoise.c   \
                $(RNNOISE_SRC)/rnn.c       \
                $(RNNOISE_SRC)/nnet.c      \
                $(RNNOISE_SRC)/nnet_default.c \
                $(RNNOISE_SRC)/kiss_fft.c  \
                $(RNNOISE_SRC)/pitch.c     \
                $(RNNOISE_SRC)/celt_lpc.c  \
                $(RNNOISE_SRC)/rnnoise_data.c \
                $(RNNOISE_SRC)/rnnoise_tables.c \
                $(RNNOISE_SRC)/parse_lpcnet_weights.c

RNNOISE_OBJS := $(patsubst $(RNNOISE_SRC)/%.c,$(OBJ_DIR)/rnnoise_%.o,$(RNNOISE_SRCS))

# ---- Mozart source files -----------------------------------------------------
MOZART_SRCS  := $(SRC_DIR)/stage.c    \
                $(SRC_DIR)/pipeline.c \
                $(SRC_DIR)/rnnoise.c  \
                $(SRC_DIR)/pipewire.c \
                $(SRC_DIR)/pre.c

MOZART_OBJS  := $(patsubst $(SRC_DIR)/%.c,$(OBJ_DIR)/mozart_%.o,$(MOZART_SRCS))

# ---- Example -----------------------------------------------------------------
EXAMPLE_SRC   := $(SRC_DIR)/main.c
EXAMPLE_OBJ   := $(OBJ_DIR)/mozart_main.o

# ============================================================================
# Rules
# ============================================================================

.PHONY: all debug stub clean run help
all: CFLAGS := $(CFLAGS_RELEASE) -DMOZART_USE_RNNOISE=1
all: $(BIN_EXAMPLE)

debug: CFLAGS := $(CFLAGS_DEBUG) -DMOZART_USE_RNNOISE=1
debug: $(BIN_EXAMPLE)

stub: CFLAGS := $(CFLAGS_STUB)
stub: $(BIN_EXAMPLE)

run: all
	@$(BIN_EXAMPLE)

help:
	@echo "Mozart Preprocessing Pipeline — Makefile"
	@echo ""
	@echo "  make [all]     Release build with RNNoise (default)"
	@echo "  make debug     Debug build with symbols"
	@echo "  make stub      Build without RNNoise (passthrough stub)"
	@echo "  make clean     Remove build artifacts"
	@echo "  make run       Build and run smoke test"
	@echo "  make help      Show this help"

# ---- RNNoise static library --------------------------------------------------
$(LIB_RNNOISE): $(RNNOISE_OBJS)
	@$(MKDIR) $(LIB_DIR)
	$(AR) rcs $@ $^

$(OBJ_DIR)/rnnoise_%.o: $(RNNOISE_SRC)/%.c
	@$(MKDIR) $(OBJ_DIR)
	$(CC) $(CFLAGS) -fno-fast-math -DRNNOISE_BUILD -DHAVE_CONFIG_H=1 -DTRAINING=0 -c $< -o $@

# ---- Mozart static library ---------------------------------------------------
$(LIB_MOZART): $(MOZART_OBJS)
	@$(MKDIR) $(LIB_DIR)
	$(AR) rcs $@ $^

$(OBJ_DIR)/mozart_%.o: $(SRC_DIR)/%.c
	@$(MKDIR) $(OBJ_DIR)
	$(CC) $(CFLAGS) -c $< -o $@

# ---- Example executable ------------------------------------------------------
$(BIN_EXAMPLE): $(EXAMPLE_OBJ) $(LIB_MOZART) $(LIB_RNNOISE)
	@$(MKDIR) $(BIN_DIR)
	$(CC) $(CFLAGS) $< -L$(LIB_DIR) -lmozart_pre -lrnnoise $(LDFLAGS) -o $@

$(OBJ_DIR)/mozart_main.o: $(SRC_DIR)/main.c
	@$(MKDIR) $(OBJ_DIR)
	$(CC) $(CFLAGS) -c $< -o $@

# ---- Clean -------------------------------------------------------------------
clean:
	$(RM) $(BUILD_DIR)
