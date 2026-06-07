#include <QQmlApplicationEngine>
#include "GlossaInjector.hpp"

extern "C" void registerGlossa() {
    qmlRegisterSingletonInstance<GlossaInjector>(
            "dev.glossa.injector", 0, 1,
            "GlossaInjector", new GlossaInjector());
}
