#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>

static BOOL gProductReturn;
static BOOL gProductThrows;
static NSException *gProductException;
static BOOL gCollectorThrowsBefore;
static BOOL gCollectorThrowsReturned;
static BOOL gCollectorThrowsThrew;
static NSInteger gProductCalls;
static NSInteger gCollectorBeforeCalls;
static NSInteger gCollectorReturnedCalls;
static NSInteger gCollectorThrewCalls;
static NSInteger gCollectorFailureCalls;
static NSInteger gSuperclassCalls;

@interface RLAutomaticRecorder : NSObject
@end

@interface RLTestApplication : UIApplication
@end

@implementation RLAutomaticRecorder
+ (BOOL)bootstrap {
    return YES;
}

+ (id)_rlWillSendAction:(SEL)action
                      to:(id)target
                    from:(id)sender
                forEvent:(UIEvent *)event {
    (void)action;
    (void)target;
    (void)sender;
    (void)event;
    gCollectorBeforeCalls += 1;
    if (gCollectorThrowsBefore) {
        [NSException raise:@"CollectorBefore" format:@"collector failure"];
    }
    return [NSObject new];
}

+ (void)_rlActionReturned:(id)token {
    (void)token;
    gCollectorReturnedCalls += 1;
    if (gCollectorThrowsReturned) {
        [NSException raise:@"CollectorAfter" format:@"collector failure"];
    }
}

+ (void)_rlActionThrew:(id)token {
    (void)token;
    gCollectorThrewCalls += 1;
    if (gCollectorThrowsThrew) {
        [NSException raise:@"CollectorAfterThrow" format:@"collector failure"];
    }
}

+ (void)_rlCollectorFailure {
    gCollectorFailureCalls += 1;
}
@end

@implementation UIApplication
+ (instancetype)sharedApplication {
    static UIApplication *application;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        application = [RLTestApplication new];
    });
    return application;
}

- (BOOL)sendAction:(SEL)action
                to:(id)target
              from:(id)sender
          forEvent:(UIEvent *)event {
    (void)action;
    (void)target;
    (void)sender;
    (void)event;
    gSuperclassCalls += 1;
    gProductCalls += 1;
    if (gProductThrows) {
        @throw gProductException;
    }
    return gProductReturn;
}
@end

@implementation RLTestApplication
- (BOOL)sendAction:(SEL)action
                to:(id)target
              from:(id)sender
          forEvent:(UIEvent *)event {
    (void)action;
    (void)target;
    (void)sender;
    (void)event;
    // Deliberately do not call super. The shim must hook this concrete class.
    gProductCalls += 1;
    if (gProductThrows) {
        @throw gProductException;
    }
    return gProductReturn;
}
@end

// Include the production shim in this translation unit so the harness can
// invoke the actual typed IMP hook without a simulator or generated header.
#define REPRO_AUTO_DEBUG 1
#include "../../../../reproof/ios_instrumentation_templates/RLAutoBootstrap.m"

static int Check(BOOL condition, const char *message) {
    if (condition) {
        return 0;
    }
    fprintf(stderr, "FAIL: %s\n", message);
    return 1;
}

static void ResetCollector(void) {
    gCollectorThrowsBefore = NO;
    gCollectorThrowsReturned = NO;
    gCollectorThrowsThrew = NO;
    gCollectorBeforeCalls = 0;
    gCollectorReturnedCalls = 0;
    gCollectorThrewCalls = 0;
    gCollectorFailureCalls = 0;
}

int main(void) {
    @autoreleasepool {
        RLInstallSendActionHook();
        UIApplication *application = [UIApplication sharedApplication];
        int failures = 0;

        ResetCollector();
        gProductCalls = 0;
        gProductThrows = NO;
        gProductReturn = NO;
        BOOL falseResult = [application sendAction:@selector(run)
                                                to:nil
                                              from:nil
                                          forEvent:nil];
        failures += Check(falseResult == NO, "false BOOL return changed");
        failures += Check(gProductCalls == 1, "false action dispatched more than once");

        ResetCollector();
        gProductCalls = 0;
        gProductReturn = YES;
        BOOL trueResult = [application sendAction:@selector(run)
                                               to:nil
                                             from:nil
                                         forEvent:nil];
        failures += Check(trueResult == YES, "true BOOL return changed");
        failures += Check(gProductCalls == 1, "true action dispatched more than once");
        failures += Check(gCollectorBeforeCalls == 1 && gCollectorReturnedCalls == 1,
                          "normal collector callbacks were not paired");

        ResetCollector();
        gProductCalls = 0;
        gProductReturn = NO;
        gCollectorThrowsBefore = YES;
        falseResult = [application sendAction:@selector(run)
                                           to:nil
                                         from:nil
                                     forEvent:nil];
        failures += Check(falseResult == NO, "collector-before exception changed false BOOL");
        failures += Check(gProductCalls == 1, "collector-before exception suppressed product dispatch");
        failures += Check(gCollectorFailureCalls == 1, "collector-before exception was not isolated");

        ResetCollector();
        gProductCalls = 0;
        gProductReturn = YES;
        gCollectorThrowsReturned = YES;
        trueResult = [application sendAction:@selector(run)
                                          to:nil
                                        from:nil
                                    forEvent:nil];
        failures += Check(trueResult == YES, "collector-after exception changed true BOOL");
        failures += Check(gProductCalls == 1, "collector-after exception changed dispatch count");
        failures += Check(gCollectorFailureCalls == 1, "collector-after exception was not isolated");

        ResetCollector();
        gProductCalls = 0;
        gProductThrows = YES;
        gProductException = [NSException exceptionWithName:@"ProductFailure"
                                                       reason:@"product exception"
                                                     userInfo:nil];
        gCollectorThrowsThrew = YES;
        NSException *caught = nil;
        @try {
            (void)[application sendAction:@selector(run)
                                        to:nil
                                      from:nil
                                  forEvent:nil];
        } @catch (NSException *exception) {
            caught = exception;
        }
        failures += Check(gProductCalls == 1, "throwing action dispatched more than once");
        failures += Check(caught == gProductException, "original NSException identity changed");
        failures += Check(gCollectorThrewCalls == 1 && gCollectorFailureCalls == 1,
                          "collector-after-throw exception was not isolated");
        failures += Check(gSuperclassCalls == 0,
                          "hook mutated or dispatched through UIApplication superclass");

        if (failures == 0) {
            puts("PASS");
        }
        return failures == 0 ? 0 : 1;
    }
}
