package com.grocery.booking.grocery.Service;

import com.grocery.booking.grocery.DTO.OrderItemRequest;
import com.grocery.booking.grocery.Entity.GroceryItem;
import com.grocery.booking.grocery.Entity.Order;
import com.grocery.booking.grocery.Entity.OrderItem;
import com.grocery.booking.grocery.Exception.ResourceNotFoundException;
import com.grocery.booking.grocery.Repository.GroceryItemRepository;
import com.grocery.booking.grocery.Repository.OrderRepository;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.List;

@Service
public class OrderService {
    @Autowired
    private OrderRepository orderRepository;
    private GroceryItemRepository groceryItemRepository;


        public Order createOrder(List<OrderItemRequest> orderItemRequests) {
            // Validate and create the order
            // ...

            // You would need to implement this method based on your business logic

            Order order = new Order();
            List<OrderItem> orderItems = new ArrayList<>();

            for (OrderItemRequest orderItemRequest : orderItemRequests) {
                GroceryItem groceryItem = groceryItemRepository.findById(orderItemRequest.getItemId())
                        .orElseThrow(() -> new ResourceNotFoundException("GroceryItem not found with id: " + orderItemRequest.getItemId()));

                if (groceryItem.getQuantity() < orderItemRequest.getQuantity()) {
                    throw new IllegalArgumentException("Not enough stock for item: " + groceryItem.getName());
                }

                OrderItem orderItem = new OrderItem();
                orderItem.setGroceryItem(groceryItem);
                orderItem.setQuantity(orderItemRequest.getQuantity());

                orderItems.add(orderItem);
            }

            order.setItems(orderItems);
            order.setTotalAmount(calculateTotalAmount(orderItems));

            return orderRepository.save(order);
        }

    private BigDecimal calculateTotalAmount(List<OrderItem> orderItems) {
        BigDecimal totalAmount = BigDecimal.ZERO;

        for (OrderItem orderItem : orderItems) {
            BigDecimal itemTotal = orderItem.getGroceryItem().getPrice()
                    .multiply(new BigDecimal(orderItem.getQuantity()));
            totalAmount = totalAmount.add(itemTotal);
        }

        return totalAmount;
    }

        
    }

