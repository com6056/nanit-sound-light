#!/bin/bash

# CI script for Nanit Sound + Light integration
# Runs formatting, linting, and proto generation

set -e  # Exit on any error

echo "🚀 Running CI checks for Nanit Sound + Light..."

# Check if we're in the right directory
if [ ! -f "custom_components/nanit_sound_light/manifest.json" ]; then
    echo "❌ Error: Run this script from the project root directory"
    exit 1
fi

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_step() {
    echo -e "${YELLOW}🔧 $1${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

# Check if required tools are installed
check_tool() {
    if ! command -v "$1" &> /dev/null; then
        print_error "$1 is not installed. Please install it first."
        echo "  For $1: $2"
        exit 1
    fi
}

print_step "Checking required tools..."
check_tool "ruff" "brew install ruff"
check_tool "prettier" "brew install prettier"
check_tool "protoc" "brew install protobuf"

# 1. Format Python code
print_step "Running ruff format..."
if ruff format .; then
    print_success "Python formatting completed"
else
    print_error "Python formatting failed"
    exit 1
fi

# 2. Lint Python code
print_step "Running ruff lint..."
if ruff check .; then
    print_success "Python linting passed"
else
    print_error "Python linting failed"
    exit 1
fi

# 3. Format JSON/MD/YAML files
print_step "Running prettier..."
if prettier --write "**/*.{json,md,yml}"; then
    print_success "Prettier formatting completed"
else
    print_error "Prettier formatting failed"
    exit 1
fi

# 4. Re-generate protobuf files
print_step "Regenerating protobuf files..."
PROTO_DIR="custom_components/nanit_sound_light"
PROTO_FILE="$PROTO_DIR/sound_light.proto"
OUTPUT_FILE="$PROTO_DIR/sound_light_pb2.py"

if [ ! -f "$PROTO_FILE" ]; then
    print_error "Proto file not found: $PROTO_FILE"
    exit 1
fi

# Generate new protobuf file
if protoc --python_out="$PROTO_DIR" --proto_path="$PROTO_DIR" "$PROTO_FILE"; then
    print_success "Protobuf generation completed"
else
    print_error "Protobuf generation failed"
    exit 1
fi

# 5. Check if protobuf file is up to date
print_step "Checking if protobuf file is up to date..."
if git diff --quiet "$OUTPUT_FILE"; then
    print_success "Protobuf file is up to date"
else
    print_error "Protobuf file is out of date!"
    echo "Generated protobuf file differs from committed version."
    echo "The protobuf file has been updated. Please commit the changes."
    git diff "$OUTPUT_FILE"
    exit 1
fi

print_success "All CI checks passed! 🎉"
echo ""
echo "Summary:"
echo "  ✅ Python formatting (ruff format)"
echo "  ✅ Python linting (ruff check)" 
echo "  ✅ File formatting (prettier)"
echo "  ✅ Protobuf generation"
echo "  ✅ Protobuf up-to-date check"
echo ""
echo "Ready for commit and release! 🚀"