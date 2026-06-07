TEMPLATE = lib
TARGET = glossa-injector
CONFIG += shared plugin no_plugin_name_prefix
QMAKE_LFLAGS += -Wl,--no-undefined

OBJECTS_DIR = build/obj
MOC_DIR = build/moc
UI_DIR = build/ui
XOVI_DIR = build/xovi

xoviextension.target = build/xovi/xovi.c
xoviextension.commands = mkdir -p $$XOVI_DIR && python3 $$(XOVI_REPO)/util/xovigen.py -o $$XOVI_DIR/xovi.c -H $$XOVI_DIR/xovi.h glossa-injector.xovi
xoviextension.depends = glossa-injector.xovi

QMAKE_EXTRA_TARGETS += xoviextension
PRE_TARGETDEPS += $$XOVI_DIR/xovi.c

QT += quick qml

CONFIG += c++17

SOURCES += \
    main.cpp entry.c $$XOVI_DIR/xovi.c \
    GlossaInjector.cpp \
    rm_Line.cpp rm_SceneLineItem.cpp

HEADERS += GlossaInjector.hpp rm_Line.hpp rm_SceneItem.hpp rm_SceneLineItem.hpp
INCLUDEPATH += $$XOVI_DIR

QMAKE_CXXFLAGS += -fPIC -Werror -Wno-invalid-offsetof -O3 -mfpu=neon -mfloat-abi=hard
QMAKE_CFLAGS += -fPIC -O3 -mfpu=neon -mfloat-abi=hard
