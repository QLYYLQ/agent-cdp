# How Qt Signals and Slots Work — Internal Implementation

> Source: https://woboq.com/blog/how-qt-signals-slots-work.html

## Core Mechanism Overview

Qt's signals and slots system enables inter-object communication through a metadata-driven architecture. It implements this using the MOC (Meta Object Compiler) to generate introspection tables and connection infrastructure.

## Magic Macros

The Qt extensions to C++ are simple preprocessor macros:

- `signals` and `slots` are defined as `public` and empty respectively, serving mainly as hints to MOC
- `emit` is completely empty — purely documentary
- `Q_OBJECT` injects static metadata, virtual methods, and translation helpers

The `SIGNAL()` and `SLOT()` macros convert function signatures to strings with type prefixes ("2" for signals, "1" for slots).

## MOC-Generated Code Structure

### QMetaObject Static Data

Each class gets a `staticMetaObject` containing:
- Pointer to parent's metaobject
- String table reference (`stringdata`)
- Integer data array describing methods/properties
- Function pointer to `qt_static_metacall`

### Integer Data Layout

The metadata array stores:
- Header (13 integers): revision, class name index, method counts and offsets
- Method descriptions (5 integers each): name index, parameter count, parameter type info, flags
- Parameter type information using `QMetaType` enums

### String Table

Strings are stored in a compact `QByteArrayData` array with `QT_MOC_LITERAL` macros providing offset calculations to avoid duplication.

### Signal Implementation

Generated signal functions create a void-pointer array where the first element is the return value (typically null), then argument pointers. This array is passed to `QMetaObject::activate()`.

### Slot Invocation

The `qt_static_metacall()` function uses a switch statement indexed by method ID, casting arguments and calling the actual slot implementation.

## Connection Infrastructure

### QObjectPrivate::Connection Structure

Each connection stores:
- Sender and receiver object pointers
- Union: either a `StaticMetaCallFunction` pointer or `QSlotObjectBase`
- Signal index (27 bits) and connection type (3 bits)
- Method offset and relative index for slot location
- Doubly-linked list pointers for sender list
- Singly-linked list pointer for receiver's connection list
- Atomic reference counting

The `prev` pointer is unusually a pointer-to-pointer, allowing O(1) removal without special cases for the first element.

### Connection Storage

Objects maintain a vector of `ConnectionList` structures indexed by signal index. Each `ConnectionList` is a linked list of `Connection` objects. Receivers maintain reverse-linked lists for cleanup on deletion.

## Signal Emission Process

When `QMetaObject::activate()` executes:

1. **Fast path check**: A 64-bit bitmask quickly determines if any slots are connected; unconnected signals return immediately
2. **Mutex locking**: The signal/slot lock protects connection list access
3. **List retrieval**: Get the appropriate `ConnectionList` for the signal index
4. **Iteration**: Traverse connections, noting the last to prevent emitting new connections added during emission

For each connection:

- **Thread check**: Compare receiver's thread with current thread
- **Connection type handling**:
  - Auto/Queued connections to different threads are posted as events
  - Direct connections invoke immediately
  - Blocking connections wait for completion
- **Sender context**: `QConnectionSenderSwitcher` temporarily sets the emitter as `sender()`
- **Invocation**: Call either the cached `callFunction` (MOC-generated `qt_static_metacall`) or fall back to `QMetaObject::metacall()` for dynamic objects
- **Safety check**: Verify the connection list wasn't orphaned (sender destroyed)

## Method Indexing

Three index types exist internally:
- **Relative index**: Within a single class, starting at 0
- **Absolute index**: Includes parent class methods via offset addition
- **Signal index**: Only counts signals, used for the connection vector (more compact than including slots)

Public API functions like `indexOf{Signal,Slot,Method}` return absolute indexes.

## Key Design Insights

- Architecture prioritizes performance for signal emission with minimal overhead when unconnected
- Connection storage uses linked lists for O(1) add/remove operations
- Dual-pointer-to-pointer pattern in sender lists eliminates special cases
- Metadata is completely static and read-only, enabling memory efficiency and quick lookups
- 64-bit bitmask provides fast-path for checking if any slots are connected to a signal
