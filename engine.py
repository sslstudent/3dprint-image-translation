import os

import torch
from torchvision.utils import make_grid, save_image

from metric import mean_pixel_loss, mean_absolute_cri_error


def set_requires_grad(nets, requires_grad=False):
    if not isinstance(nets, list):
        nets = [nets]
    for net in nets:
        if net is not None:
            for param in net.parameters():
                param.requires_grad = requires_grad


def train_one_epoch(G: torch.nn.Module, D: torch.nn.Module, optimG: torch.nn.Module, optimD: torch.nn.Module,
                    schedG, schedD,
                    adv_loss, l1_loss, vgg_loss, train_loader: torch.utils.data.DataLoader, epoch: int,
                    device: torch.device, log_writer: torch.utils.tensorboard.SummaryWriter, args):

    G.train()
    D.train()

    if epoch > args.n_epochs:
        schedG.step()
        schedD.step()

    for iter, (real_A, real_B, cond) in enumerate(train_loader, 1):

        real_A = real_A.to(device)
        real_B = real_B.to(device)
        cond = cond.to(device)

        with torch.cuda.amp.autocast():
            fake_B = G(real_A, cond)  # G(A)
            set_requires_grad(D, True)  # enable backprop for D
            optimD.zero_grad()  # set D's gradients to zero
            fake_AB = torch.cat((real_A, fake_B),
                                1)  # we use conditional GANs; we need to feed both input and output to the discriminator
            pred_fake = D(fake_AB.detach(), cond, G.condition_embedding.embeddings)
            loss_D_fake = adv_loss(pred_fake, False)
            real_AB = torch.cat((real_A, real_B), 1)
            pred_real = D(real_AB.detach(), cond, G.condition_embedding.embeddings)
            loss_D_real = adv_loss(pred_real, True)
            loss_D = (loss_D_fake + loss_D_real) * 0.5
        loss_D.backward()
        optimD.step()  # update D's weights

        with torch.cuda.amp.autocast():
            # update G
            set_requires_grad(D, False)  # D requires no gradients when optimizing G
            optimG.zero_grad()  # set G's gradients to zero
            fake_AB = torch.cat((real_A, fake_B), 1)
            pred_fake = D(fake_AB, cond, G.condition_embedding.embeddings)
            loss_G_GAN = adv_loss(pred_fake, True)
            loss_G_VGG = vgg_loss(fake_B, real_B) * args.lambda_VGG
            loss_G_L1  = l1_loss(fake_B, real_B) * args.lambda_L1
            loss_G = loss_G_GAN + loss_G_VGG + loss_G_L1
        loss_G.backward()
        optimG.step()  # udpate G's weights

        global_step = (epoch - 1) * len(train_loader) + iter

        if not (iter % args.log_freq) or (iter == len(train_loader)):
            loss_G_adv = loss_G_GAN.item()
            loss_G_vgg = loss_G_VGG.item()
            loss_G_l1  = loss_G_L1.item()
            loss_D_real = loss_D_real.item()
            loss_D_fake = loss_D_fake.item()

            print(f"Epoch [{epoch}/{args.total_epochs}] Batch [{iter}/{len(train_loader)}] "
                  f"G_adv: {loss_G_adv:.4f} G_vgg: {loss_G_vgg:.4f} G_l1: {loss_G_l1:.4f} "
                  f"D_real: {loss_D_real:.4f} D_fake: {loss_D_fake:.4f} ")

            log_writer.add_scalar('G/adversarial', loss_G_adv, global_step)
            log_writer.add_scalar('G/vgg', loss_G_vgg, global_step)
            log_writer.add_scalar('G/l1', loss_G_l1, global_step)
            log_writer.add_scalar('G/lr', optimG.param_groups[0]['lr'], global_step)

            log_writer.add_scalar('D/real', loss_D_real, global_step)
            log_writer.add_scalar('D/fake', loss_D_fake, global_step)
            log_writer.add_scalar('D/lr', optimD.param_groups[0]['lr'], global_step)

        if not (iter % args.display_freq):
            log_writer.add_image('images/src', make_grid(real_A, nrow=2, value_range=(-1, 1), normalize=True), global_step)
            log_writer.add_image('images/dst', make_grid(real_B, nrow=2, value_range=(-1, 1), normalize=True), global_step)
            log_writer.add_image('images/gen', make_grid(fake_B, nrow=2, value_range=(-1, 1), normalize=True), global_step)


@torch.no_grad()
def evaluate(G: torch.nn.Module, data_loader: torch.utils.data.DataLoader, epoch: int, device: torch.device,
             log_writer: torch.utils.tensorboard.SummaryWriter, args):

    print("=" * 80)
    print(f"Validation at epoch [{epoch}/{args.total_epochs}]")

    G.eval()
    true_images = []
    fake_images = []
    for src, dst, cond in data_loader:
        src = src.to(device)
        cond = cond.to(device)
        fake = G(src, cond)
        fake_images.append(fake.detach())
        true_images.append(dst.detach())

    fake_images = (torch.cat(fake_images) + 1) * 0.5 * 255.
    fake_images = fake_images.to("cpu").squeeze()

    true_images = (torch.cat(true_images) + 1) * 0.5 * 255.
    true_images = true_images.to("cpu").squeeze()

    pix_error = mean_pixel_loss(fake_images, true_images)
    cri_error, cri_error_array = mean_absolute_cri_error(fake_images, true_images, return_error_array=True)

    log_writer.add_scalar("metric/pixel loss", pix_error, global_step=epoch)
    log_writer.add_scalar("metric/cri_error", cri_error, global_step=epoch)
    log_writer.add_histogram("metric/cri_error_hist", cri_error_array, global_step=epoch)

    print(f"* Mean Pix error: {pix_error:.4f}")
    print(f"* Mean CRI error: {cri_error:.4f}")
    print(f"* # of samples (CRI error < 20): {(cri_error_array<20).sum()} / {len(data_loader.dataset)}")
    print("=" * 80)

    return pix_error, cri_error


@torch.no_grad()
def generate(G: torch.nn.Module, data_loader: torch.utils.data.DataLoader, epoch: int, device: torch.device,
             save_images: bool, log_writer, args):
    G.eval()
    fake_images = []
    for src, cond in data_loader:
        src = src.to(device)
        cond = cond.to(device)
        fake = G(src, cond)
        fake_images.append(fake.detach())
    fake_images = torch.cat(fake_images)

    if log_writer is not None:
        log_writer.add_image('images/test_gen', make_grid(fake_images, nrow=2, value_range=(-1, 1), normalize=True), epoch)

    if save_images:
        fake_images = (fake_images + 1.0) * 0.5  # (-1, 1) -> (0, 1)
        for org_filename, fake_img in zip(data_loader.dataset.src_images, fake_images):
            save_path = os.path.join(args.output_dir, os.path.basename(org_filename))
            save_image(fake_img, save_path)

    return fake_images