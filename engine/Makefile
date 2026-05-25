CXX      = g++
CXXFLAGS = -std=c++17 -O2 -Wall -Wextra
TARGET   = build/v4s
SRC      = src/v4s.cpp

.PHONY: all clean

all: build $(TARGET)

build:
	mkdir -p build

$(TARGET): $(SRC)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SRC)

clean:
	rm -f $(TARGET)
