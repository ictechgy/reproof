# MixedShop

간편 모드 빌드 파이프라인 검증용 앱이다. XcodeGen(`project.yml`)으로 프로젝트를 만들고,
UIKit 탭 + UIKit에 끼운 SwiftUI + 순수 SwiftUI 탭 + 로컬 패키지(`Packages/ShopKit`)를 섞었다.
접근성 ID는 "비우기" 버튼의 `cart.clear` 하나뿐이다(ID 없는 회사 앱을 흉내 낸 것).

- bundle id: `io.reproof.example.mixedshop`, scheme: `MixedShop`, 테스트 scheme: `MixedShopTests`
- 생성된 `.xcodeproj`는 커밋하지 않는다.
