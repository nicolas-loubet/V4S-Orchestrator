CXX      = g++
CXXFLAGS = -std=c++17 -O3 -DDIFF_RESID_MOLECULE
TARGET   = build/v4s
SRC      = src/main.cpp

.PHONY: all clean

all: build $(TARGET)

build:
	mkdir -p build

$(TARGET): $(SRC)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SRC)

clean:
	rm -f $(TARGET)
