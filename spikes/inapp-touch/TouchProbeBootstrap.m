// Spike S2(버림용, 테스트 없음): 앱 시작 전에 TouchProbe(Swift)를 설치하는 +load 진입점. Debug 빌드에서만 컴파일된다.
#if DEBUG
#import <Foundation/Foundation.h>
#import <objc/message.h>

/// Swift `MixedShopTouchProbe.install`을 런타임 이름으로 찾아 부른다.
/// 왜 이름으로 찾나: 이 파일이 Swift 생성 헤더에 기대지 않게 하려는 것이다(`RLAutoBootstrap.m`과 같은 방식).
@interface MixedShopTouchProbeBootstrap : NSObject
@end

@implementation MixedShopTouchProbeBootstrap
/// 앱 이미지가 올라올 때(main 전, main thread) 한 번 불린다. 프로브가 없으면 원인을 로그로 남긴다.
+ (void)load {
    Class probe = NSClassFromString(@"MixedShopTouchProbe");
    SEL install = NSSelectorFromString(@"install");
    if (probe == nil || ![probe respondsToSelector:install]) {
        NSLog(@"[TouchProbe] MixedShopTouchProbe.install을 찾지 못했습니다. "
              @"TouchProbe.swift가 같은 앱 타깃의 Debug 빌드에 들어갔는지 확인하세요.");
        return;
    }
    ((void (*)(id, SEL))objc_msgSend)(probe, install);
}
@end
#endif
