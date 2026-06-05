#include <QQmlApplicationEngine>
#include "GrimoireInjector.hpp"

extern "C" void registerGrimoire() {
    qmlRegisterSingletonInstance<GrimoireInjector>(
            "dev.grimoire.injector", 0, 1,
            "GrimoireInjector", new GrimoireInjector());
}
