# Qt's Meta-Object System

> Source: https://doc.qt.io/qt-6/metaobjects.html

## Overview

Qt's meta-object system provides foundational infrastructure for inter-object communication, runtime type information, and dynamic properties.

## Core Components

The system relies on three essential elements:

1. **QObject Class**: Provides a base class enabling meta-object system features
2. **Q_OBJECT Macro**: Activates meta-object capabilities including dynamic properties, signals, and slots
3. **Meta-Object Compiler (moc)**: Processes C++ source files containing the Q_OBJECT macro and generates implementation code

## How the Meta-Object Compiler Works

The `moc` tool scans C++ source files for class declarations containing the Q_OBJECT macro. When found, it produces a corresponding C++ source file containing meta-object code for those classes. This generated file is either included directly in the class implementation or compiled and linked separately.

## Key Features

Beyond signals and slots communication, the meta-object system provides:

- **Runtime class information** via `QObject::metaObject()` and `QMetaObject::className()`
- **Inheritance checking** through `QObject::inherits()`
- **String translation** using `QObject::tr()`
- **Dynamic property access** with `QObject::setProperty()` and `QObject::property()`
- **Instance construction** via `QMetaObject::newInstance()`

## Dynamic Casting

The `qobject_cast()` function enables safe type casting without requiring C++ RTTI support. It returns a non-zero pointer for correct types or `nullptr` for incompatible objects, functioning across dynamic library boundaries.

## Recommendation

All QObject subclasses should include the Q_OBJECT macro regardless of actual usage, ensuring accurate class identification and full feature availability.
